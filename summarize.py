#!/usr/bin/env python3
"""
summarize.py: step 3 of the meeting pipeline.

Turns a timestamped transcript into a structured summary: an overview,
decisions (motions and votes), discussion topics, public comments, next
steps, and passages that were hard to hear. Every item links to the second
in the video where it happened.

How it works (v2):
  1. Topic pass: the transcript is split into chunks (about 8 minutes each
     for local models) and each chunk is summarized. Motions and votes are
     deliberately left out of this pass.
  2. Vote pass: code (not AI) searches the whole transcript for motion and
     vote language ("motion", "second", "all in favor", "opposed", "died"...),
     groups nearby hits into events, and the model reads each event with a
     few minutes of context before it.
  3. Overview pass: written last, from the decisions list, so it can't
     contradict them.
  4. Checks: every timestamp and quote is matched against the transcript,
     vote counts and "unanimous" must actually be said, and the overview's
     vote counts must match the decisions. Anything that fails is flagged.

Two interchangeable AI backends:
  ollama  (default) free, runs a local model on your GPU. No account needed.
  claude  the Claude API. Better summaries; needs ANTHROPIC_API_KEY in .env.

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

QUOTE_WINDOW_SECONDS = 90     # how far from its timestamp a quote may appear
VOTE_CONTEXT_BEFORE = 150     # seconds of transcript the model sees before a vote
VOTE_CONTEXT_AFTER = 30
VOTE_MERGE_GAP = 120          # vote-language hits closer than this are one event

# Words that signal a formal motion or vote. "second" only counts on its own
# ("Second." / "I'll second") so "the second question" doesn't trigger it.
VOTE_CUES = re.compile(
    r"\b(motion|so moved|seconded|i'?ll second|second(?=[.!?]|$)|do (i|we) have a second|"
    r"all in favor|all opposed|any opposed|opposed|aye|nay|carries|carried|"
    r"died for lack|unanimous(ly)?)\b",
    re.IGNORECASE,
)


# ---------- prompts and schemas ----------

BASE_RULES = """You summarize public local government meetings for the residents \
of Blue Ridge and Fannin County, Georgia. Residents rely on your summary instead of \
watching the meeting, so accuracy matters more than completeness.

Rules:
- Use only what is said in the transcript. Never add outside knowledge or assumptions.
- Each transcript line starts with [hh:mm:ss | seconds]. For every item, set \
timestamp_seconds to the seconds value of the line where it happens. Only use \
seconds values that appear in the transcript.
- "quote" must be copied word for word from that line or the lines right after it \
(under 25 words), and it must directly support the item. Do not paraphrase inside a quote.
- Name people only if their name is said in the transcript. Never guess a person's \
role (resident, commissioner, staff) unless the transcript says it. Otherwise say \
"a speaker".
- Never explain why someone voted or felt a certain way unless they said why.
- The transcript is machine-made from audio that is sometimes quiet or has people \
talking over each other. If a passage is garbled or contradictory, add it to \
unclear_audio instead of guessing.
- Neutral, plain language. No opinions about officials or decisions."""

CHUNK_RULES = BASE_RULES + """

For this task:
- Do NOT report motions or votes. They are handled separately.
- discussions: the main topics, presentations, and questions in this part.
- public_comments: ONLY people who come forward to speak during a public comment \
period or public hearing (for example after "would anyone like to come speak in \
favor" or "in opposition"). Board members, commissioners, and staff asking questions \
or debating are discussions, not public comments. If nobody from the public speaks, \
return an empty list.
- upcoming: ONLY things that will happen after this meeting (future meetings, \
hearings, deadlines, or "this goes to City Council next"). Not items from this meeting."""

VOTE_RULES = BASE_RULES + """

For this task you get an excerpt of the transcript around possible motion or vote \
language. List every FORMAL motion in the excerpt that was made and then voted on, \
withdrawn, or died for lack of a second. Ignore casual uses of words like "second" \
or "motion" that are not formal motions. If there are none, return an empty list.
- motion: what was moved, in plain words (for example "Rezone the parcel to C2 with \
staff conditions").
- outcome: "passed", "failed", "died for lack of a second", "withdrawn", "tabled", or \
"unclear" if the result isn't clearly stated.
- vote_count: only if numbers are actually said (for example "3-1"). Use "unanimous" \
only if that word is said. Otherwise leave it empty. Hearing "aye" alone is NOT \
enough for "unanimous".
- timestamp_seconds: the line where the vote result is stated (or where the motion \
died)."""

OVERVIEW_RULES = BASE_RULES + """

For this task, write a 2-4 sentence overview of the whole meeting using ONLY the \
part summaries and the decisions list you are given.
- Report each decision's outcome and vote count exactly as listed. If an outcome is \
"unclear", say the result was unclear. Do not add vote counts that aren't listed.
- Do not say "unanimous" unless a listed vote_count says unanimous.
- Do not give reasons for any vote.
- Mention listed next steps."""

TIMESTAMP = {"type": "integer", "description": "seconds value from the transcript line"}
QUOTE = {"type": "string", "description": "exact words from the transcript, under 25 words"}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def array_of(properties: dict) -> dict:
    return {"type": "array", "items": obj(properties)}


CHUNK_SCHEMA = obj({
    "overview": {"type": "string", "description": "2-3 sentences on what this part covered"},
    "discussions": array_of({"topic": {"type": "string"}, "summary": {"type": "string"},
                             "timestamp_seconds": TIMESTAMP, "quote": QUOTE}),
    "public_comments": array_of({
        "speaker": {"type": "string", "description": "name if stated, else empty"},
        "position": {"type": "string", "enum": ["in favor", "opposed", "neutral", "unclear"]},
        "summary": {"type": "string"}, "timestamp_seconds": TIMESTAMP, "quote": QUOTE}),
    "upcoming": array_of({"item": {"type": "string"}, "timestamp_seconds": TIMESTAMP}),
    "unclear_audio": array_of({"description": {"type": "string"},
                               "timestamp_seconds": TIMESTAMP}),
    "keywords": {"type": "array", "items": {"type": "string"},
                 "description": "places, roads, businesses, topics"},
})

VOTE_SCHEMA = obj({
    "votes": array_of({
        "motion": {"type": "string"},
        "outcome": {"type": "string", "enum": ["passed", "failed", "died for lack of a second",
                                               "withdrawn", "tabled", "unclear"]},
        "vote_count": {"type": "string", "description": "e.g. '3-1' or 'unanimous'; empty if not said"},
        "timestamp_seconds": TIMESTAMP,
        "quote": QUOTE,
    }),
})

OVERVIEW_SCHEMA = obj({"overview": {"type": "string"}})


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


def meeting_header(meeting: dict) -> str:
    return (f"Meeting: {meeting.get('title', 'Unknown')}\n"
            f"Board: {meeting.get('board', 'Unknown')}\n"
            f"Date: {meeting.get('meeting_date', 'Unknown')}\n")


def segments_between(segments, start: float, end: float):
    return [s for s in segments if start <= s["start"] <= end]


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


class TokenCounter:
    def __init__(self, backend):
        self.backend, self.tokens_in, self.tokens_out, self.calls = backend, 0, 0, 0

    def complete(self, system, prompt, schema):
        result, (t_in, t_out) = self.backend.complete(system, prompt, schema)
        self.tokens_in, self.tokens_out, self.calls = (self.tokens_in + t_in,
                                                       self.tokens_out + t_out, self.calls + 1)
        return result


# ---------- pass 1: topics, chunk by chunk ----------

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


def topic_pass(ai, meeting, segments, chunk_chars):
    chunks = chunk_segments(segments, chunk_chars)
    merged = {"part_overviews": [], "discussions": [], "public_comments": [],
              "upcoming": [], "unclear_audio": [], "keywords": []}
    for i, chunk in enumerate(chunks, 1):
        print(f"  Topics {i}/{len(chunks)} [{hms(chunk[0]['start'])}-{hms(chunk[-1]['end'])}]...",
              flush=True)
        where = (f"This is part {i} of {len(chunks)} of the meeting. Summarize only this part.\n"
                 if len(chunks) > 1 else "")
        prompt = f"{meeting_header(meeting)}{where}\nTranscript:\n{transcript_for_prompt(chunk)}"
        part = ai.complete(CHUNK_RULES, prompt, CHUNK_SCHEMA)
        merged["part_overviews"].append(
            f"[{hms(chunk[0]['start'])}-{hms(chunk[-1]['end'])}] {part['overview']}")
        for key in ("discussions", "public_comments", "upcoming", "unclear_audio", "keywords"):
            merged[key].extend(part.get(key, []))
    seen = set()
    merged["keywords"] = [k for k in merged["keywords"]
                          if not (k.lower() in seen or seen.add(k.lower()))]
    return merged


# ---------- pass 2: votes, found by code ----------

def find_vote_events(segments):
    """Group transcript lines containing motion/vote language into events.
    Returns a list of (first_cue_time, last_cue_time)."""
    cue_times = [s["start"] for s in segments if VOTE_CUES.search(s["text"])]
    events = []
    for t in cue_times:
        if events and t - events[-1][1] <= VOTE_MERGE_GAP:
            events[-1][1] = t
        else:
            events.append([t, t])
    return [tuple(e) for e in events]


def vote_pass(ai, meeting, segments, part_overviews):
    events = find_vote_events(segments)
    agenda = "\n".join(part_overviews)
    decisions = []
    for i, (first, last) in enumerate(events, 1):
        window = segments_between(segments, first - VOTE_CONTEXT_BEFORE, last + VOTE_CONTEXT_AFTER)
        print(f"  Votes {i}/{len(events)} [{hms(first)}-{hms(last)}]...", flush=True)
        prompt = (f"{meeting_header(meeting)}\nWhat happened in the meeting, part by part "
                  f"(for context only):\n{agenda}\n\nExcerpt to examine:\n"
                  f"{transcript_for_prompt(window)}")
        result = ai.complete(VOTE_RULES, prompt, VOTE_SCHEMA)
        window_words = set(words(" ".join(s["text"] for s in window)))
        for vote in result.get("votes", []):
            vote["_window_words"] = window_words
            # Skip duplicates: the same vote found again in an overlapping window.
            if any(abs(vote["timestamp_seconds"] - d["timestamp_seconds"]) <= 30 and
                   difflib.SequenceMatcher(None, vote["motion"].lower(),
                                           d["motion"].lower()).ratio() > 0.6
                   for d in decisions):
                continue
            decisions.append(vote)
    decisions.sort(key=lambda d: d["timestamp_seconds"])
    return decisions, len(events)


# ---------- pass 3: overview, written last ----------

def overview_pass(ai, meeting, part_overviews, decisions, upcoming):
    decision_lines = "\n".join(
        f"- [{hms(d['timestamp_seconds'])}] {d['motion']}: {d['outcome']}"
        + (f" ({d['vote_count']})" if d["vote_count"] else "")
        for d in decisions) or "- No formal votes were found."
    next_steps = "\n".join(f"- {u['item']}" for u in upcoming) or "- None listed."
    prompt = (f"{meeting_header(meeting)}\nPart summaries:\n" + "\n".join(part_overviews)
              + f"\n\nDecisions (authoritative):\n{decision_lines}\n\nNext steps:\n{next_steps}")
    return ai.complete(OVERVIEW_RULES, prompt, OVERVIEW_SCHEMA)["overview"]


# ---------- pass 4: checks ----------

def quote_found(needle, haystack) -> bool:
    """Exact word match, or a close match (allows small transcription
    differences like 'gonna' vs 'going to')."""
    if " ".join(needle) in " ".join(haystack):
        return True
    n = len(needle)
    for i in range(max(1, len(haystack) - n + 1)):
        if difflib.SequenceMatcher(None, needle, haystack[i:i + n]).ratio() >= 0.85:
            return True
    return False


def check_item(item, segments, starts) -> list:
    notes = []
    ts = item.get("timestamp_seconds", -1)
    if not any(abs(ts - st) <= 2 for st in starts):
        notes.append("timestamp doesn't match a transcript line")
    elif item.get("quote"):
        nearby = [s for s in segments if abs(s["start"] - ts) <= QUOTE_WINDOW_SECONDS]
        if not quote_found(words(item["quote"]), words(" ".join(s["text"] for s in nearby))):
            notes.append("quote not found near timestamp")
    return notes


def check_vote(vote) -> list:
    """Vote counts and 'unanimous' must actually be said near the vote."""
    notes, said = [], vote.pop("_window_words", set())
    count = vote.get("vote_count", "").strip()
    if count:
        if "unanimous" in count.lower():
            if not {"unanimous", "unanimously"} & said:
                notes.append("'unanimous' was never said; removed")
                vote["vote_count"] = ""
        else:
            missing = [n for n in re.findall(r"\d+", count) if n not in said]
            if missing or not re.findall(r"\d+", count):
                notes.append(f"vote count '{count}' wasn't said; removed")
                vote["vote_count"] = ""
    return notes


def check_overview(overview: str, decisions) -> list:
    """The overview may only repeat vote counts that appear in the decisions."""
    notes = []
    listed = {re.sub(r"\s+", "", d["vote_count"]).replace("–", "-")
              for d in decisions if d["vote_count"]}
    for count in re.findall(r"\b\d+\s*[-–]\s*\d+\b", overview):
        if re.sub(r"\s+", "", count).replace("–", "-") not in listed:
            notes.append(f"overview mentions vote '{count}' that isn't in the decisions")
    if re.search(r"\bunanimous", overview, re.I) and not any(
            "unanimous" in d["vote_count"].lower() for d in decisions):
        notes.append("overview says 'unanimous' but no decision does")
    return notes


def run_checks(summary, segments) -> int:
    starts = sorted(int(s["start"]) for s in segments)
    failures = 0
    for section in ("decisions", "discussions", "public_comments", "upcoming", "unclear_audio"):
        for item in summary[section]:
            notes = check_vote(item) if section == "decisions" else []
            notes += check_item(item, segments, starts)
            item["verified"] = not notes
            item["verify_note"] = "; ".join(notes)
            failures += bool(notes)
    notes = check_overview(summary["overview"], summary["decisions"])
    summary["overview_verified"] = not notes
    summary["overview_note"] = "; ".join(notes)
    return failures + bool(notes)


# ---------- output ----------

def write_markdown(path: Path, meeting: dict, summary: dict) -> None:
    url = meeting.get("url", "")

    def link(ts):
        return f"[{hms(ts)}]({url}&t={ts}s)" if url else f"[{hms(ts)}]"

    def flag(item):
        return "" if item.get("verified", True) else f" ⚠️ *{item['verify_note']}*"

    lines = [f"# {meeting.get('title', path.stem)}", ""]
    if meeting.get("board") or meeting.get("meeting_date"):
        lines += [f"**{meeting.get('board', '')}** · {meeting.get('meeting_date', '')}", ""]
    lines += ["## Overview", "", summary["overview"]]
    if not summary["overview_verified"]:
        lines.append(f"\n⚠️ *{summary['overview_note']}*")
    lines.append("")

    lines += ["## Decisions", ""]
    if summary["decisions"]:
        for d in summary["decisions"]:
            vote = f" ({d['vote_count']})" if d["vote_count"] else ""
            lines.append(f"- **{d['outcome'].capitalize()}{vote}:** {d['motion']} "
                         f"{link(d['timestamp_seconds'])}{flag(d)}")
            lines.append(f"  > \"{d['quote']}\"")
    else:
        lines.append("- No formal votes were found in this meeting.")
    lines.append("")

    if summary["discussions"]:
        lines += ["## Discussion", ""]
        for d in summary["discussions"]:
            lines.append(f"- **{d['topic']}:** {d['summary']} {link(d['timestamp_seconds'])}{flag(d)}")
        lines.append("")
    lines += ["## Public comments", ""]
    if summary["public_comments"]:
        for c in summary["public_comments"]:
            who = c["speaker"] or "A speaker"
            lines.append(f"- **{who}** ({c['position']}): {c['summary']} "
                         f"{link(c['timestamp_seconds'])}{flag(c)}")
    else:
        lines.append("- No public comments.")
    lines.append("")
    if summary["upcoming"]:
        lines += ["## Next steps", ""]
        lines += [f"- {u['item']} {link(u['timestamp_seconds'])}{flag(u)}" for u in summary["upcoming"]]
        lines.append("")
    if summary["unclear_audio"]:
        lines += ["## Hard to hear", ""]
        lines += [f"- {u['description']} {link(u['timestamp_seconds'])}" for u in summary["unclear_audio"]]
        lines.append("")
    if summary["keywords"]:
        lines += [f"*Keywords: {', '.join(summary['keywords'])}*", ""]
    meta = summary["_meta"]
    lines.append(f"*AI-generated from the meeting recording by {meta['model']}. "
                 f"{meta['unverified_items']} of {meta['items'] + 1} items failed automatic checks. "
                 f"Click any timestamp to check the source.*")
    path.write_text("\n".join(lines) + "\n")


# ---------- main ----------

def summarize(ai, meeting, segments, chunk_chars):
    topics = topic_pass(ai, meeting, segments, chunk_chars)
    decisions, vote_events = vote_pass(ai, meeting, segments, topics["part_overviews"])
    print("  Overview...", flush=True)
    overview = overview_pass(ai, meeting, topics["part_overviews"], decisions, topics["upcoming"])
    return {
        "overview": overview,
        "decisions": decisions,
        "discussions": topics["discussions"],
        "public_comments": topics["public_comments"],
        "upcoming": topics["upcoming"],
        "unclear_audio": topics["unclear_audio"],
        "keywords": topics["keywords"],
        "part_overviews": topics["part_overviews"],
    }, vote_events


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

        ai = TokenCounter(backend)
        started = time.time()
        try:
            summary, vote_events = summarize(ai, meeting, segments, chunk_chars)
        except Exception as err:
            print(f"  [!] {err}")
            continue
        elapsed = time.time() - started

        failures = run_checks(summary, segments)
        items = sum(len(summary[k]) for k in
                    ("decisions", "discussions", "public_comments", "upcoming", "unclear_audio"))
        summary["_meta"] = {
            "video_id": vid, "backend": args.backend, "model": model, "version": 2,
            "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(elapsed, 1), "ai_calls": ai.calls,
            "input_tokens": ai.tokens_in, "output_tokens": ai.tokens_out,
            "vote_events_found": vote_events, "items": items, "unverified_items": failures,
        }
        json_path = SUMMARIES_DIR / f"{vid}.json"
        md_path = SUMMARIES_DIR / f"{vid}.md"
        json_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_markdown(md_path, meeting, summary)

        print(f"  {len(summary['decisions'])} decisions (from {vote_events} vote events), "
              f"{len(summary['discussions'])} discussion topics, "
              f"{len(summary['public_comments'])} public comments")
        print(f"  Took {hms(elapsed)}; {ai.calls} AI calls; "
              f"{ai.tokens_in:,} tokens in / {ai.tokens_out:,} out")
        print(f"  Checks: {failures} of {items + 1} item(s) flagged" if failures
              else f"  Checks: all {items + 1} items passed")
        print(f"  Saved {md_path.relative_to(HERE)} and {json_path.name}")

        if vid in meetings:
            meetings[vid]["status"] = "summarized"
            meetings[vid]["summary"] = str(json_path.relative_to(HERE))
            MEETINGS_FILE.write_text(json.dumps(meetings, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
