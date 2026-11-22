#!/usr/bin/env python3
"""
summarize.py: step 3 of the meeting pipeline.

Turns a timestamped transcript into a structured summary (overview,
decisions and votes, discussions, public comments, upcoming items). Every
item points at the second in the video where it happened, and every quote is
checked against the transcript so made-up details get flagged instead of
published.

Two interchangeable AI backends:
  ollama  (default) free, runs a local model on your GPU. No account needed.
  claude  the Claude API. Better summaries; needs ANTHROPIC_API_KEY in .env.

Long meetings are split into chunks (about 8 minutes each for local models),
summarized piece by piece, then merged into one summary.

Usage:
  python summarize.py                        summarize every meeting marked "transcribed"
  python summarize.py --video-id ID          summarize (or re-summarize) one meeting
  python summarize.py --model qwen3.5:4b     pick a different local model
  python summarize.py --backend claude       use the Claude API instead

Output:
  summaries/<id>.json   structured summary (used by the website later)
  summaries/<id>.md     readable version with clickable YouTube timestamps
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
TRANSCRIPTS_DIR = HERE / "transcripts"
SUMMARIES_DIR = HERE / "summaries"
ENV_FILE = HERE / ".env"

DEFAULTS = {
    "ollama": {"model": "qwen3.5:4b", "chunk_chars": 9000},
    "claude": {"model": "claude-sonnet-5-5", "chunk_chars": 400000},
}
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

SYSTEM_PROMPT = """You summarize public local government meetings for the residents \
of Blue Ridge and Fannin County, Georgia. Residents rely on your summary instead of \
watching the meeting, so accuracy matters more than completeness.

Rules:
- Use only what is said in the transcript. Never add outside knowledge or assumptions.
- Each transcript line starts with [hh:mm:ss | seconds]. For every item, set \
timestamp_seconds to the seconds value of the line where it happens. Only use \
seconds values that appear in the transcript.
- "quote" must be copied word for word from that line or the lines right after it \
(under 25 words). Do not paraphrase inside a quote.
- Only report a vote count if the count or "unanimous" is actually said. Otherwise \
leave "vote" empty. Do not guess outcomes; use "unclear" if you can't tell.
- Name people only if their name is said in the transcript.
- The transcript is machine-made from audio that is sometimes quiet or has people \
talking over each other. If a passage is garbled or contradictory, add it to \
unclear_audio instead of guessing what was meant.
- Neutral, plain language. No opinions about officials or decisions."""

ITEM_TIMESTAMP = {"type": "integer", "description": "seconds value from the transcript line"}
ITEM_QUOTE = {"type": "string", "description": "exact words from the transcript, under 25 words"}

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string", "description": "2-4 sentence summary of the meeting"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "outcome": {"type": "string",
                                "enum": ["approved", "denied", "tabled", "continued",
                                         "recommended", "no action", "unclear"]},
                    "vote": {"type": "string", "description": "e.g. '4-1' or 'unanimous'; empty if not stated"},
                    "timestamp_seconds": ITEM_TIMESTAMP,
                    "quote": ITEM_QUOTE,
                },
                "required": ["description", "outcome", "vote", "timestamp_seconds", "quote"],
                "additionalProperties": False,
            },
        },
        "discussions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                    "timestamp_seconds": ITEM_TIMESTAMP,
                    "quote": ITEM_QUOTE,
                },
                "required": ["topic", "summary", "timestamp_seconds", "quote"],
                "additionalProperties": False,
            },
        },
        "public_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "description": "name if stated, else empty"},
                    "summary": {"type": "string"},
                    "timestamp_seconds": ITEM_TIMESTAMP,
                    "quote": ITEM_QUOTE,
                },
                "required": ["speaker", "summary", "timestamp_seconds", "quote"],
                "additionalProperties": False,
            },
        },
        "upcoming": {
            "type": "array",
            "description": "future meetings, hearings, or deadlines that were announced",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "timestamp_seconds": ITEM_TIMESTAMP,
                },
                "required": ["item", "timestamp_seconds"],
                "additionalProperties": False,
            },
        },
        "unclear_audio": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "timestamp_seconds": ITEM_TIMESTAMP,
                },
                "required": ["description", "timestamp_seconds"],
                "additionalProperties": False,
            },
        },
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "5-15 search terms: places, roads, businesses, topics"},
    },
    "required": ["overview", "decisions", "discussions", "public_comments",
                 "upcoming", "unclear_audio", "keywords"],
    "additionalProperties": False,
}

SECTIONS_WITH_QUOTES = ["decisions", "discussions", "public_comments"]
QUOTE_WINDOW_SECONDS = 90  # how far from the timestamp a quote may appear


# ---------- helpers ----------

def load_env() -> None:
    """Read KEY=value lines from .env into the environment (no extra package)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def words(text: str):
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def transcript_for_prompt(segments) -> str:
    return "\n".join(f"[{hms(s['start'])} | {int(s['start'])}] {s['text']}" for s in segments)


# ---------- verification ----------

def verify(summary: dict, segments) -> int:
    """Check each item's timestamp and quote against the transcript.
    Adds 'verified' (bool) and 'verify_note' to every item. Returns the
    number of items that failed."""
    starts = sorted(int(s["start"]) for s in segments)
    failures = 0
    for section in SECTIONS_WITH_QUOTES + ["upcoming", "unclear_audio"]:
        for item in summary.get(section, []):
            ts = item.get("timestamp_seconds", -1)
            note = ""
            if not any(abs(ts - st) <= 2 for st in starts):
                note = "timestamp doesn't match a transcript line"
            elif "quote" in item:
                nearby = [s for s in segments if abs(s["start"] - ts) <= QUOTE_WINDOW_SECONDS]
                haystack = words(" ".join(s["text"] for s in nearby))
                needle = words(item["quote"])
                if needle and not quote_found(needle, haystack):
                    note = "quote not found near timestamp"
            item["verified"] = not note
            item["verify_note"] = note
            failures += bool(note)
    return failures


def quote_found(needle, haystack) -> bool:
    """Exact word match, or a close match (allows small transcription
    differences like 'gonna' vs 'going to')."""
    n = len(needle)
    if " ".join(needle) in " ".join(haystack):
        return True
    best = 0.0
    for i in range(max(1, len(haystack) - n + 1)):
        window = haystack[i:i + n]
        best = max(best, difflib.SequenceMatcher(None, needle, window).ratio())
        if best >= 0.85:
            return True
    return False


# ---------- output ----------

def write_markdown(path: Path, meeting: dict, summary: dict) -> None:
    url = meeting.get("url", "")

    def link(ts):
        return f"[{hms(ts)}]({url}&t={ts}s)" if url else f"[{hms(ts)}]"

    def flag(item):
        return "" if item.get("verified", True) else f" ⚠️ *unverified: {item['verify_note']}*"

    lines = [f"# {meeting.get('title', path.stem)}", ""]
    if meeting.get("board") or meeting.get("meeting_date"):
        lines += [f"**{meeting.get('board', '')}** · {meeting.get('meeting_date', '')}", ""]
    lines += ["## Overview", "", summary["overview"], ""]

    if summary["decisions"]:
        lines += ["## Decisions", ""]
        for d in summary["decisions"]:
            vote = f" ({d['vote']})" if d["vote"] else ""
            lines.append(f"- **{d['outcome'].title()}{vote}:** {d['description']} "
                         f"{link(d['timestamp_seconds'])}{flag(d)}")
            lines.append(f"  > \"{d['quote']}\"")
        lines.append("")
    if summary["discussions"]:
        lines += ["## Discussion", ""]
        for d in summary["discussions"]:
            lines.append(f"- **{d['topic']}:** {d['summary']} {link(d['timestamp_seconds'])}{flag(d)}")
        lines.append("")
    if summary["public_comments"]:
        lines += ["## Public comments", ""]
        for c in summary["public_comments"]:
            who = c["speaker"] or "A resident"
            lines.append(f"- **{who}:** {c['summary']} {link(c['timestamp_seconds'])}{flag(c)}")
        lines.append("")
    if summary["upcoming"]:
        lines += ["## Coming up", ""]
        lines += [f"- {u['item']} {link(u['timestamp_seconds'])}" for u in summary["upcoming"]]
        lines.append("")
    if summary["unclear_audio"]:
        lines += ["## Hard to hear", ""]
        lines += [f"- {u['description']} {link(u['timestamp_seconds'])}" for u in summary["unclear_audio"]]
        lines.append("")
    if summary["keywords"]:
        lines += [f"*Keywords: {', '.join(summary['keywords'])}*", ""]
    lines.append("*AI-generated from the meeting recording. Click any timestamp to check the source.*")
    path.write_text("\n".join(lines) + "\n")


# ---------- main ----------

# ---------- AI backends ----------

class OllamaBackend:
    """Local model through Ollama's HTTP API (standard library only)."""

    def __init__(self, model: str):
        self.model = model
        self.think_supported = True

    def complete(self, system: str, prompt: str, schema: dict):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "format": schema,           # constrains output to the JSON schema
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 3000},
        }
        if self.think_supported:
            body["think"] = False       # skip "reasoning" text; we only want JSON
        try:
            data = self._post(body)
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            if "think" in detail and self.think_supported:
                self.think_supported = False   # model has no thinking mode
                return self.complete(system, prompt, schema)
            if "not found" in detail:
                raise RuntimeError(f"model '{self.model}' isn't downloaded. Run: ollama pull {self.model}")
            raise RuntimeError(f"Ollama error: {detail}")
        except urllib.error.URLError:
            raise RuntimeError("can't reach Ollama. Start it with: sudo systemctl start ollama")
        if data.get("done_reason") == "length":
            raise RuntimeError("output was cut off; try a smaller --chunk-chars")
        return json.loads(data["message"]["content"]), (
            data.get("prompt_eval_count", 0), data.get("eval_count", 0))

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read())


class ClaudeBackend:
    """The Claude API, for when the project can afford it."""

    def __init__(self, model: str):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic()

    def complete(self, system: str, prompt: str, schema: dict):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason == "max_tokens":
            raise RuntimeError("summary was cut off (hit max_tokens)")
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to summarize this transcript")
        text = next(block.text for block in response.content if block.type == "text")
        return json.loads(text), (response.usage.input_tokens, response.usage.output_tokens)


# ---------- chunking and merging ----------

OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {"overview": {"type": "string"}},
    "required": ["overview"],
    "additionalProperties": False,
}


def chunk_segments(segments, max_chars: int):
    """Split transcript lines into consecutive chunks of about max_chars."""
    chunks, current, size = [], [], 0
    for seg in segments:
        line_len = len(seg["text"]) + 20
        if current and size + line_len > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(seg)
        size += line_len
    if current:
        chunks.append(current)
    return chunks


def meeting_header(meeting: dict) -> str:
    return (f"Meeting: {meeting.get('title', 'Unknown')}\n"
            f"Board: {meeting.get('board', 'Unknown')}\n"
            f"Date: {meeting.get('meeting_date', 'Unknown')}\n")


def summarize(backend, meeting: dict, segments, chunk_chars: int):
    chunks = chunk_segments(segments, chunk_chars)
    parts, tokens_in, tokens_out = [], 0, 0
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  Part {i}/{len(chunks)} [{hms(chunk[0]['start'])}-{hms(chunk[-1]['end'])}]...",
                  flush=True)
        where = (f"This is part {i} of {len(chunks)} of the meeting. Summarize only this part.\n"
                 if len(chunks) > 1 else "")
        prompt = f"{meeting_header(meeting)}{where}\nTranscript:\n{transcript_for_prompt(chunk)}"
        part, (t_in, t_out) = backend.complete(SYSTEM_PROMPT, prompt, SCHEMA)
        parts.append(part)
        tokens_in, tokens_out = tokens_in + t_in, tokens_out + t_out

    if len(parts) == 1:
        return parts[0], (tokens_in, tokens_out)

    merged = {key: [] for key in SCHEMA["required"] if key != "overview"}
    for part in parts:
        for key in merged:
            merged[key].extend(part.get(key, []))
    seen = set()
    merged["keywords"] = [k for k in merged["keywords"]
                          if not (k.lower() in seen or seen.add(k.lower()))]

    # One more short call turns the per-part overviews into a single overview.
    part_overviews = "\n".join(f"Part {i}: {p['overview']}" for i, p in enumerate(parts, 1))
    prompt = (f"{meeting_header(meeting)}\nThese are summaries of consecutive parts of one "
              f"meeting. Write a single 2-4 sentence overview of the whole meeting using only "
              f"this information.\n\n{part_overviews}")
    result, (t_in, t_out) = backend.complete(SYSTEM_PROMPT, prompt, OVERVIEW_SCHEMA)
    merged["overview"] = result["overview"]
    return merged, (tokens_in + t_in, tokens_out + t_out)


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize meeting transcripts.")
    parser.add_argument("--video-id", help="summarize one meeting (even if already summarized)")
    parser.add_argument("--backend", choices=DEFAULTS, default="ollama",
                        help="ollama (free, local) or claude (paid API)")
    parser.add_argument("--model", help="model name (default depends on backend)")
    parser.add_argument("--chunk-chars", type=int,
                        help="transcript characters per chunk (default depends on backend)")
    args = parser.parse_args()
    model = args.model or DEFAULTS[args.backend]["model"]
    chunk_chars = args.chunk_chars or DEFAULTS[args.backend]["chunk_chars"]

    load_env()
    if args.backend == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("[!] No API key. Create a .env file in this folder containing:")
        print("    ANTHROPIC_API_KEY=sk-ant-...")
        return 1

    meetings = json.loads(MEETINGS_FILE.read_text()) if MEETINGS_FILE.exists() else {}
    if args.video_id:
        ids = [args.video_id]
    else:
        ids = [k for k, m in meetings.items() if m.get("status") == "transcribed"]
    if not ids:
        print("Nothing to summarize. Transcribe a meeting first.")
        return 0

    backend = OllamaBackend(model) if args.backend == "ollama" else ClaudeBackend(model)
    print(f"Using {args.backend} / {model}")
    SUMMARIES_DIR.mkdir(exist_ok=True)

    for vid in ids:
        transcript_path = TRANSCRIPTS_DIR / f"{vid}.json"
        if not transcript_path.exists():
            print(f"[!] No transcript for {vid}; skipping.")
            continue
        meeting = meetings.get(vid, {"title": vid})
        segments = json.loads(transcript_path.read_text())["segments"]
        print(f"\nSummarizing: {meeting.get('title', vid)} ({len(segments)} transcript lines)")

        started = time.time()
        try:
            summary, (tokens_in, tokens_out) = summarize(backend, meeting, segments, chunk_chars)
        except Exception as err:
            print(f"  [!] {err}")
            continue
        elapsed = time.time() - started

        failures = verify(summary, segments)
        total = sum(len(summary.get(k, [])) for k in
                    SECTIONS_WITH_QUOTES + ["upcoming", "unclear_audio"])
        summary["_meta"] = {
            "video_id": vid,
            "backend": args.backend,
            "model": model,
            "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(elapsed, 1),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "items": total,
            "unverified_items": failures,
        }
        json_path = SUMMARIES_DIR / f"{vid}.json"
        md_path = SUMMARIES_DIR / f"{vid}.md"
        json_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_markdown(md_path, meeting, summary)

        print(f"  {len(summary['decisions'])} decisions, {len(summary['discussions'])} discussion "
              f"topics, {len(summary['public_comments'])} public comments")
        print(f"  Took {hms(elapsed)}; {tokens_in:,} tokens in / {tokens_out:,} out")
        print(f"  Verification: {failures} of {total} item(s) flagged" if failures
              else f"  Verification: all {total} items checked out")
        print(f"  Saved {md_path.relative_to(HERE)} and {json_path.name}")

        if vid in meetings:
            meetings[vid]["status"] = "summarized"
            meetings[vid]["summary"] = str(json_path.relative_to(HERE))
            MEETINGS_FILE.write_text(json.dumps(meetings, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
