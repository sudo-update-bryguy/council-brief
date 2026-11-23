#!/usr/bin/env python3
"""
summarize.py: step 3 of the meeting pipeline.

Turns a timestamped transcript into a structured summary: an overview,
decisions (motions and votes), discussion topics, public comments, next
steps, and passages that were hard to hear. Every item links to the second
in the video where it happened.

How it works (v3):
  1. Topic pass: the transcript is split into chunks (about 8 minutes each
     for local models) and each chunk is summarized. Motions and votes are
     deliberately left out of this pass.
  2. Vote pass: code (not AI) searches the whole transcript for motion and
     vote language ("motion", "second", "all in favor", "opposed", "died"...),
     groups nearby hits into events, and the model reads each event with a
     few minutes of context before it.
  3. Overview pass: written last, from the decisions list, so it can't
     contradict them.
  4. Checks: the model points at numbered transcript lines (code converts
     them to exact seconds, or finds the quote if the line is wrong), every
     quote is matched against the transcript, vote counts and "unanimous" must
     actually be said, "passed"/"failed" needs voting language nearby, and the
     overview's vote counts must match the decisions. Anything that fails is
     flagged.

Three interchangeable AI backends:
  ollama  (default) free, runs a local model on your GPU. No account needed.
  gemini  Google's Gemini API free tier; needs GEMINI_API_KEY in .env.
  claude  the Claude API (paid); needs ANTHROPIC_API_KEY in .env.

Usage:
  python summarize.py                        summarize every meeting marked "transcribed"
  python summarize.py --video-id ID          summarize (or re-summarize) one meeting
  python summarize.py --model qwen3.5:4b     pick a different local model
  python summarize.py --backend gemini       use Gemini's free tier instead
  python summarize.py --tag gemini           save as summaries/<id>.gemini.md (for comparisons)

Output:
  summaries/<id>.json   structured summary (used by the website later)
  summaries/<id>.md     readable version with clickable YouTube timestamps
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
TRANSCRIPTS_DIR = HERE / "transcripts"
SUMMARIES_DIR = HERE / "summaries"
ENV_FILE = HERE / ".env"

DEFAULTS = {
    "ollama": {"model": "qwen3.5:4b", "chunk_chars": 9000},
    "gemini": {"model": "gemini-3.6-flash", "chunk_chars": 400000},
    "claude": {"model": "claude-sonnet-5-5", "chunk_chars": 400000},
}
API_KEYS = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

QUOTE_WINDOW_SECONDS = 90     # how far from its timestamp a quote may appear
VOTE_CONTEXT_BEFORE = 150     # seconds of transcript the model sees before a vote
VOTE_CONTEXT_AFTER = 30
VOTE_MERGE_GAP = 120          # vote-language hits closer than this are one event

# Words that signal a formal motion or vote. "second" only counts on its own
# ("Second." / "I'll second") so "the second question" doesn't trigger it.
DIED_CUE = re.compile(r"died for lack|lack of a second|no second", re.IGNORECASE)
RESULT_CUES = re.compile(
    r"\b(all in favor|aye|ayes|opposed|nay|carries|carried|passes|passed|fails|failed|"
    r"unanimous(ly)?|\d+\s*(-|–|to)\s*\d+|\d+ \d+)\b", re.IGNORECASE)
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
- Each transcript line starts with a line number like [L12 | 00:14:03]. For every \
item, set "line" to the number of the line where it happens (12 for L12). Only use \
line numbers that appear in the excerpt you were given.
- "quote" must be copied word for word from that line or the lines right after it \
(under 25 words), and it must directly support the item. Do not paraphrase inside a quote.
- Name people only if their name is said in the transcript, or if the transcript labels \
them. Labels work like a script: "Greg Arnold: ..." means Greg Arnold is talking, and \
lines without a label are the same person as the line before. A " / " inside a line means \
someone else starts talking. The "Who's speaking" list gives roles. Voice matching can be \
wrong, so what is SAID always outweighs a label. Never turn a label like "Speaker 4" into \
a name or role yourself; call that person "a speaker". Never guess a person's role \
(resident, commissioner, staff) unless the transcript or the list says it.
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
- line: the line where the vote result is stated (or where the motion died).
- The lines code flagged as containing vote language are listed for you. Every \
"died for lack of a second" in the excerpt must appear in your list."""

OVERVIEW_RULES = BASE_RULES + """

For this task, write a 2-3 sentence overview of what the meeting covered, using ONLY \
the part summaries you are given.
- Do NOT mention any vote, motion, or outcome (passed, failed, approved, denied, died). \
The votes are listed separately, word for word from the decisions, so leave them out.
- Describe the topics, the main points raised, and who presented (by role or name, \
only if the summaries give it).
- Mention listed next steps."""

TIMESTAMP = {"type": "integer", "description": "the L number of the transcript line"}
QUOTE = {"type": "string", "description": "exact words from the transcript, under 25 words"}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def array_of(properties: dict) -> dict:
    return {"type": "array", "items": obj(properties)}


CHUNK_SCHEMA = obj({
    "overview": {"type": "string", "description": "2-3 sentences on what this part covered"},
    "discussions": array_of({"topic": {"type": "string"}, "summary": {"type": "string"},
                             "line": TIMESTAMP, "quote": QUOTE}),
    "public_comments": array_of({
        "speaker": {"type": "string", "description": "name if stated, else empty"},
        "position": {"type": "string", "enum": ["in favor", "opposed", "neutral", "unclear"]},
        "summary": {"type": "string"}, "line": TIMESTAMP, "quote": QUOTE}),
    "upcoming": array_of({"item": {"type": "string"}, "line": TIMESTAMP}),
    "unclear_audio": array_of({"description": {"type": "string"},
                               "line": TIMESTAMP}),
    "keywords": {"type": "array", "items": {"type": "string"},
                 "description": "places, roads, businesses, topics"},
})

VOTE_SCHEMA = obj({
    "votes": array_of({
        "motion": {"type": "string"},
        "outcome": {"type": "string", "enum": ["passed", "failed", "died for lack of a second",
                                               "withdrawn", "tabled", "unclear"]},
        "vote_count": {"type": "string", "description": "e.g. '3-1' or 'unanimous'; empty if not said"},
        "line": TIMESTAMP,
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


def short_name(item: dict) -> str:
    return item.get("speaker_name") or item.get("speaker") or "Speaker ?"


def transcript_for_prompt(segments) -> str:
    """Number lines L1, L2... within this excerpt. Small models copy short line
    numbers far more reliably than long timestamps; code converts them back.
    After speakers.py, the speaker is written like a script: a short name only
    when it changes, with a "who's who" list on top (so roles aren't repeated
    on every line, which wasted tokens and cluttered the text)."""
    if not any(s.get("turns") for s in segments):
        return "\n".join(f"[L{i} | {hms(s['start'])}] {s['text']}" for i, s in enumerate(segments, 1))
    who, out, current = {}, [], None
    for i, seg in enumerate(segments, 1):
        parts = []
        for turn in seg.get("turns") or [seg]:
            name = short_name(turn)
            who.setdefault(name, turn.get("speaker_display", name))
            parts.append(turn["text"] if name == current else f"{name}: {turn['text']}")
            current = name
        out.append(f"[L{i} | {hms(seg['start'])}] " + " / ".join(parts))
    legend = "; ".join(display for display in who.values())
    return f"Who's speaking (matched by voice; may be wrong): {legend}\n" + "\n".join(out)


def strip_speakers(segments):
    """For comparisons: the same transcript with all speaker information removed."""
    keep = ("start", "end", "text", "words")
    return [{k: v for k, v in s.items() if k in keep} for s in segments]


def meeting_header(meeting: dict) -> str:
    return (f"Meeting: {meeting.get('title', 'Unknown')}\n"
            f"Board: {meeting.get('board', 'Unknown')}\n"
            f"Date: {meeting.get('meeting_date', 'Unknown')}\n")


def segments_between(segments, start: float, end: float):
    return [s for s in segments if start <= s["start"] <= end]


# ---------- anchoring: line numbers and quotes -> exact seconds ----------

def locate_quote(quote: str, excerpt) -> list:
    """Return the excerpt line indexes where the quote starts (exact word match,
    or the single best close match)."""
    needle = words(quote)
    if not needle:
        return []
    flat, owner = [], []
    for i, seg in enumerate(excerpt):
        for w in words(seg["text"]):
            flat.append(w)
            owner.append(i)
    n = len(needle)
    exact = [owner[k] for k in range(len(flat) - n + 1) if flat[k:k + n] == needle]
    if exact:
        return sorted(set(exact))
    best, best_k = 0.0, None
    for k in range(max(1, len(flat) - n + 1)):
        ratio = difflib.SequenceMatcher(None, needle, flat[k:k + n]).ratio()
        if ratio > best:
            best, best_k = ratio, k
    return [owner[best_k]] if best >= 0.85 and best_k is not None else []


def anchor(item: dict, excerpt, after: int = -1) -> None:
    """Turn the model's "line" into timestamp_seconds. If the line number is
    invalid or its quote isn't there, use where the quote actually appears."""
    line = item.pop("line", None)
    notes = item.setdefault("_notes", [])
    valid = isinstance(line, int) and 1 <= line <= len(excerpt)
    idx = line - 1 if valid else None
    if item.get("quote"):
        matches = locate_quote(item["quote"], excerpt)
        near = idx is not None and any(abs(m - idx) <= 3 for m in matches)
        if matches and not near:
            # Votes come in order, so a later vote can't reuse an earlier line.
            later = [m for m in matches if m > after] or matches
            idx = later[0]
            item["anchored_by_quote"] = True
        if len(matches) > 1:
            notes.append(f"quote appears {len(matches)} times nearby; the exact moment may be off")
    item["_idx"] = idx
    item["timestamp_seconds"] = int(excerpt[idx]["start"]) if idx is not None else -1


# ---------- AI backends ----------

def post_json(url: str, body: dict, headers: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


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
        return post_json(f"{OLLAMA_URL}/api/chat", body, {})


class GeminiBackend:
    """Google's Gemini API through plain HTTP (standard library only).
    The free tier has per-minute limits, so rate-limit errors are retried."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str):
        self.model = model
        self.key = os.environ["GEMINI_API_KEY"]
        self.new_format = True   # responseFormat (current API); falls back if rejected

    @staticmethod
    def _wait(attempt: int, reason: str) -> None:
        wait = 20 * attempt
        print(f"    ({reason}; waiting {wait}s, then try {attempt + 1} of 5)", flush=True)
        time.sleep(wait)

    def complete(self, system: str, prompt: str, schema: dict):
        if self.new_format:
            config = {"temperature": 0, "responseFormat": {
                "text": {"mimeType": "APPLICATION_JSON", "schema": schema}}}
        else:
            config = {"temperature": 0, "responseMimeType": "application/json",
                      "responseJsonSchema": schema}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config}
        for attempt in range(1, 6):
            try:
                data = post_json(self.URL.format(model=self.model), body,
                                 {"x-goog-api-key": self.key}, timeout=300)
                break
            except urllib.error.HTTPError as err:
                detail = err.read().decode(errors="replace")
                # 429 = free-tier rate limit; 500/503 = Google's servers busy. Temporary.
                if err.code in (429, 500, 503) and attempt < 5:
                    reason = "free-tier rate limit" if err.code == 429 else "Gemini is busy"
                    self._wait(attempt, reason)
                    continue
                if err.code == 400 and self.new_format and "responseFormat" in detail:
                    self.new_format = False
                    return self.complete(system, prompt, schema)
                if err.code in (401, 403):
                    raise RuntimeError("Gemini rejected the API key. Check GEMINI_API_KEY in .env")
                if err.code == 404:
                    raise RuntimeError(f"model '{self.model}' not found. Check the model list in "
                                       "Google AI Studio and pass it with --model")
                raise RuntimeError(f"Gemini error {err.code}: {detail[:300]}")
            except (TimeoutError, urllib.error.URLError):
                # An overloaded server sometimes just stops answering.
                if attempt == 5:
                    raise RuntimeError("Gemini stopped responding after 5 tries; "
                                       "try again later or use another --model")
                self._wait(attempt, "no response from Gemini")
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no answer: {data.get('promptFeedback')}")
        if candidates[0].get("finishReason") == "MAX_TOKENS":
            raise RuntimeError("summary was cut off (hit max tokens)")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = data.get("usageMetadata", {})
        return json.loads(text), (usage.get("promptTokenCount", 0),
                                  usage.get("candidatesTokenCount", 0))


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


class CachedAI:
    """Wraps a backend: counts tokens, and saves every successful AI answer
    to .cache/ so a rerun after a failure (or with unchanged prompts) skips the
    steps that already worked. That saves free-tier quota and time."""

    CACHE_DIR = HERE / ".cache" / "ai"

    def __init__(self, backend, use_cache: bool = True):
        self.backend, self.use_cache = backend, use_cache
        self.tokens_in = self.tokens_out = self.calls = self.cached = 0

    def complete(self, system, prompt, schema):
        key = hashlib.sha256(json.dumps(
            [type(self.backend).__name__, self.backend.model, system, prompt, schema],
            sort_keys=True).encode()).hexdigest()
        path = self.CACHE_DIR / f"{key}.json"
        if self.use_cache and path.exists():
            self.cached += 1
            return json.loads(path.read_text())
        result, (t_in, t_out) = self.backend.complete(system, prompt, schema)
        self.tokens_in, self.tokens_out, self.calls = (self.tokens_in + t_in,
                                                       self.tokens_out + t_out, self.calls + 1)
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result))
        return result


# ---------- pass 1: topics, chunk by chunk ----------

def chunk_segments(segments, max_chars: int):
    """Split transcript lines into consecutive chunks of about max_chars."""
    chunks, current, size = [], [], 0
    for seg in segments:
        # Text, line number and time, plus speaker names where they change.
        line_len = len(seg["text"]) + 20 + sum(len(short_name(t)) + 3 for t in seg.get("turns", []))
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
        for key in ("discussions", "public_comments", "upcoming", "unclear_audio"):
            for item in part.get(key, []):
                anchor(item, chunk)
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


def outcome_notes(vote: dict, excerpt) -> list:
    """'Passed'/'failed' need voting language near the stated result, and
    'died' needs 'died for lack of a second' (or similar). Otherwise the
    outcome becomes 'unclear'."""
    idx = vote.get("_idx")
    if idx is None:
        return []
    # The result usually comes a few lines after the motion ("Second." "All in
    # favor?" "Aye."), so look from 10 seconds before to 30 seconds after.
    t = excerpt[idx]["start"]
    nearby = " ".join(s["text"] for s in excerpt if t - 10 <= s["start"] <= t + 30)
    outcome = vote["outcome"]
    if outcome in ("passed", "failed") and not RESULT_CUES.search(nearby):
        vote["outcome"] = "unclear"
        vote["_no_result"] = True
        return [f"no vote result was said near this line; '{outcome}' changed to 'unclear'"]
    if outcome == "died for lack of a second" and not DIED_CUE.search(nearby):
        vote["outcome"] = "unclear"
        return ["nobody said the motion died near this line; changed to 'unclear'"]
    return []


def vote_pass(ai, meeting, segments, part_overviews):
    events = find_vote_events(segments)
    agenda = "\n".join(part_overviews)
    decisions = []
    for i, (first, last) in enumerate(events, 1):
        excerpt = segments_between(segments, first - VOTE_CONTEXT_BEFORE, last + VOTE_CONTEXT_AFTER)
        cue_lines = [(n, s) for n, s in enumerate(excerpt, 1) if VOTE_CUES.search(s["text"])]
        print(f"  Votes {i}/{len(events)} [{hms(first)}-{hms(last)}]...", flush=True)
        flagged = "\n".join(f"L{n}: {s['text']}" for n, s in cue_lines)
        prompt = (f"{meeting_header(meeting)}\nWhat happened in the meeting, part by part "
                  f"(for context only):\n{agenda}\n\nExcerpt to examine:\n"
                  f"{transcript_for_prompt(excerpt)}\n\n"
                  f"Lines code flagged as containing vote language:\n{flagged}")
        result = ai.complete(VOTE_RULES, prompt, VOTE_SCHEMA)
        said = set(words(" ".join(s["text"] for s in excerpt)))
        event_votes, after = [], -1
        for vote in result.get("votes", []):
            anchor(vote, excerpt, after)
            if vote["_idx"] is not None:
                after = vote["_idx"]
            vote["_notes"] += check_vote(vote, said) + outcome_notes(vote, excerpt)
            if vote["outcome"] == "died for lack of a second":
                vote["_excerpt"] = excerpt
            event_votes.append(vote)

        # Code safety net: every "died for lack of a second" must be accounted for.
        for idx, seg in enumerate(excerpt):
            if DIED_CUE.search(seg["text"]) and not any(
                    v["outcome"] == "died for lack of a second" and v["_idx"] is not None
                    and abs(v["_idx"] - idx) <= 3 for v in event_votes):
                event_votes.append({
                    "motion": "A motion (the AI did not identify which one)",
                    "outcome": "died for lack of a second", "vote_count": "",
                    "timestamp_seconds": int(seg["start"]), "quote": seg["text"][:150],
                    "_idx": idx, "_notes": ["added by code: the AI missed this"],
                    "_added": True, "_excerpt": excerpt})

        for vote in event_votes:
            # Skip duplicates: the same vote found again in an overlapping window.
            if any(abs(vote["timestamp_seconds"] - d["timestamp_seconds"]) <= 30 and
                   difflib.SequenceMatcher(None, vote["motion"].lower(),
                                           d["motion"].lower()).ratio() > 0.6
                   for d in decisions):
                continue
            decisions.append(vote)
    decisions.sort(key=lambda d: d["timestamp_seconds"])
    decisions = match_died_motions(ai, meeting, decisions)
    return decisions, len(events)


MATCH_RULES = BASE_RULES + """

For this task: someone said a motion died for lack of a second. Decide which of the \
listed earlier motions it was, using only the transcript excerpt. Answer with that \
motion's number, or 0 if the excerpt doesn't make it clear."""

MATCH_SCHEMA = obj({"motion_number": {"type": "integer"}})


NOT_DISTINCTIVE = set("""rezone rezoning the property parcel motion approve approval and with from
land partial request staff conditions district for this that make made move""".split())


def distinctive(text: str) -> set:
    """Words that tell one motion from another ("c2", "cbd", "hotel")."""
    return {w for w in words(text) if w not in NOT_DISTINCTIVE
            and (len(w) >= 3 or any(ch.isdigit() for ch in w))}


def voted_on_soon_after(motion: dict, decisions, t: float):
    """Is a motion with the same subject voted on (passed/failed) within 3
    minutes after time t? Then it can't be the motion that died at t."""
    named = distinctive(motion["motion"])
    return [d for d in decisions if d is not motion and d["outcome"] in ("passed", "failed")
            and 0 < d["timestamp_seconds"] - t <= 180 and named & distinctive(d["motion"])]


def match_died_motions(ai, meeting, decisions):
    """Settle which motion each "died for lack of a second" belongs to.

    Every "died" item, whether the model or code found it, is matched with a
    narrow multiple-choice question against earlier motions that never got a
    result (small models choose far better than they read open-ended).

    In testing, the model also pulled a "Rezone to C2" motion out of the CBD
    discussion, and the matcher picked it as the one that died, though the
    C2 rezoning passed a minute later. A motion that gets a real vote right
    after can't be the one that died, so such motions aren't offered as
    choices. The same rule strips a "died" label the model wrote itself."""
    for died in [d for d in decisions if d["outcome"] == "died for lack of a second"
                 and d.get("_excerpt")]:
        # Any earlier motion still marked "unclear", whether the AI said so or
        # code downgraded it, could be the one that died.
        open_motions = [d for d in decisions if d is not died and d["outcome"] == "unclear"
                        and d["timestamp_seconds"] < died["timestamp_seconds"]
                        and not voted_on_soon_after(d, decisions, died["timestamp_seconds"])]
        matched = None
        if open_motions:
            excerpt = [s for s in died["_excerpt"]
                       if abs(s["start"] - died["timestamp_seconds"]) <= 90]
            choices = "\n".join(f"{n}. [{hms(d['timestamp_seconds'])}] {d['motion']} "
                                 f"(\"{d['quote']}\")" for n, d in enumerate(open_motions, 1))
            prompt = (f"{meeting_header(meeting)}\nEarlier motions with no clear result:\n"
                      f"{choices}\n\nExcerpt where a motion died:\n{transcript_for_prompt(excerpt)}")
            print("  Matching the motion that died...", flush=True)
            n = ai.complete(MATCH_RULES, prompt, MATCH_SCHEMA).get("motion_number", 0)
            if 1 <= n <= len(open_motions):
                matched = open_motions[n - 1]
        if matched:
            made_at = hms(matched["timestamp_seconds"])
            matched.update({"outcome": "died for lack of a second",
                            "timestamp_seconds": died["timestamp_seconds"],
                            "quote": died["quote"], "_idx": None, "_no_result": False,
                            "_notes": [n for n in matched["_notes"] if "changed to 'unclear'" not in n],
                            "_info": f"motion made at {made_at}; matched by AI to this "
                                     f"'died for lack of a second'"})
            decisions.remove(died)
            continue
        if died.get("_added"):
            continue
        # Not matched: does the motion it names get a real vote within 3 minutes?
        later = voted_on_soon_after(died, decisions, died["timestamp_seconds"])
        if later:
            died["_info"] = (f"the AI named this '{died['motion']}', but that motion got a vote "
                             f"at {hms(later[0]['timestamp_seconds'])}, so the name was removed")
            died["motion"] = "A motion (the AI did not identify which one)"
            died["_added"] = True   # shown as "a motion the summary couldn't identify"
    for d in decisions:
        d.pop("_excerpt", None)
        d.pop("_no_result", None)
    return sorted(decisions, key=lambda d: d["timestamp_seconds"])


# ---------- pass 3: overview, written last ----------

def overview_pass(ai, meeting, part_overviews, upcoming):
    """The model writes only the "what was discussed" part of the overview."""
    next_steps = "\n".join(f"- {u['item']}" for u in upcoming) or "- None listed."
    prompt = (f"{meeting_header(meeting)}\nPart summaries:\n" + "\n".join(part_overviews)
              + f"\n\nNext steps:\n{next_steps}")
    return ai.complete(OVERVIEW_RULES, prompt, OVERVIEW_SCHEMA)["overview"]


# The overview's vote line is written by code from the decisions list. In
# testing, the 4B model given a correct list still swapped outcomes between
# motions ("the CBD motion failed... a C2 motion died"), and no check on its
# prose could reliably catch that. Code can't swap them.
OUTCOME_TEXT = {"passed": "passed", "failed": "failed",
                "died for lack of a second": "died for lack of a second",
                "withdrawn": "withdrawn", "unclear": "result unclear from the recording"}
ROUTINE = re.compile(r"\b(minutes|agenda)\b", re.I)


def votes_line(decisions) -> str:
    if not decisions:
        return "No formal votes were found."
    routine = [d for d in decisions if d["outcome"] == "passed" and ROUTINE.search(d["motion"])
               and len(d["motion"]) < 80]
    parts = []
    if routine:
        what = sorted({m.lower() for d in routine for m in ROUTINE.findall(d["motion"])},
                      reverse=True)   # "minutes", then "agenda"
        parts.append(f"Approved the {' and '.join(what)}.")
    for d in decisions:
        if d in routine:
            continue
        motion = d["motion"].strip().rstrip(".")
        if d.get("_added") or motion.startswith("A motion (the AI"):
            motion = "A motion the summary couldn't identify"
        count = f" ({d['vote_count']})" if d["vote_count"] else ""
        official = d.get("official", {})
        if official.get("count") and official["count"] not in ("all in favor", d["vote_count"]):
            count = f" ({official['count']} per the official record)"
        parts.append(f"{motion}: {OUTCOME_TEXT.get(d['outcome'], d['outcome'])}{count}.")
    return " ".join(parts)


VOTE_TALK = re.compile(r"\b(pass(ed|es)?|fail(ed|s)?|approved|denied|rejected|died|carried|"
                       r"adopted|tabled|unanimous(ly)?|vot(e|ed|es|ing)|motion|seconded|"
                       r"\d+\s*[-–]\s*\d+)\b", re.I)


def strip_vote_talk(prose: str):
    """Backstop: drop any sentence of the model's overview that still talks about votes."""
    sentences = re.split(r"(?<=[.!?])\s+", prose.strip())
    kept = [x for x in sentences if not VOTE_TALK.search(x)]
    removed = [x for x in sentences if VOTE_TALK.search(x)]
    # If the opening sentence was dropped, the new opener may lean on it
    # ("The council also reviewed..."); drop that "also" in its first few words.
    if kept and removed and sentences[0] in removed:
        kept[0] = re.sub(r"^((?:\S+\s+){0,3}?)also\s+", r"\1", kept[0], count=1)
    return " ".join(kept), removed


# ---------- pass 4: who said it (code, from the voice-labeled transcript) ----------

def board_roster(board: str) -> dict:
    """Officials who normally attend this board's meetings (roster.json): name -> title."""
    path = HERE / "roster.json"
    roster = json.loads(path.read_text()) if path.exists() else {}
    entry = roster.get(board, {})
    entry = roster.get(entry["same_as"], {}) if "same_as" in entry else entry
    return {p["name"]: p["title"] for p in entry.get("people", [])}


def voice_at(turns, t: float):
    """The voice that talks most from 2 s before to 20 s after t: (name, role) or None.
    Only names that passed speakers.py's evidence checks are stored, so a name
    here is never a guess."""
    talk = defaultdict(float)
    info = {}
    for turn in turns:
        overlap = min(turn["end"], t + 20) - max(turn["start"], t - 2)
        if overlap > 0 and turn.get("speaker"):
            talk[turn["speaker"]] += overlap
            info[turn["speaker"]] = (turn.get("speaker_name") or "", turn.get("speaker_role") or "")
    if not talk:
        return None
    return info[max(talk, key=talk.get)]


# Descriptions the model puts in the "speaker" field instead of a name. On the
# Nov 3 test it called three speakers "a resident"; one was the City Clerk.
ROLE_WORDS = set("""a an the resident residents speaker speakers citizen member members of
public commenter attendee audience person man woman gentleman lady applicant commissioner
commissioners councilman councilwoman council staff unknown unidentified unnamed local
homeowner owner business representative""".split())


def is_name(who: str) -> bool:
    """'Alexis Badnick', 'Rich' and 'Commissioner Barber' are names; 'a resident' isn't."""
    return any(w[0].isupper() and w.lower() not in ROLE_WORDS
               for w in re.findall(r"[A-Za-z][A-Za-z'.-]*", who))


def official_named(who: str, officials: dict):
    """Does this name refer to someone on the board's roster? Matches on surname."""
    last = who.split()[-1].lower() if who.split() else ""
    return next((n for n in officials if len(last) > 2 and n.split()[-1].lower() == last), None)


def credit_speakers(summary, labeled_segments, board: str) -> dict:
    """Public comment is for the public. Code, not the model, decides:
      - a "public comment" from someone on the roster (by the name the model
        gave, or by an evidence-checked voice name) moves to Discussion;
      - an unnamed public comment gets the voice's name, if that voice was
        named with evidence. A name the model wrote is never overwritten."""
    officials = board_roster(board)
    turns = [t for s in labeled_segments for t in (s.get("turns") or [])]
    moved = named = 0
    keep = []
    for c in summary["public_comments"]:
        voice = voice_at(turns, c["timestamp_seconds"]) if turns else None
        voice_name = voice[0] if voice else ""
        who = c.get("speaker", "").strip()
        if who and not is_name(who):
            c["_info"] = f"the summary described this speaker as '{who}', which isn't a name"
            c["speaker"] = who = ""
        # Voice matching only decides for comments the model left unnamed: a
        # timestamp a few seconds off can land on the chair, not the commenter.
        official = official_named(who, officials) or (
            voice_name if not who and voice_name in officials else None)
        if official:
            how = "the name given" if official_named(who, officials) else "voice matching"
            summary["discussions"].append({
                "topic": f"Remarks from {official} ({officials[official]})",
                "summary": c["summary"], "timestamp_seconds": c["timestamp_seconds"],
                "quote": c.get("quote", ""), "_notes": c.get("_notes", []),
                "_info": f"listed as a public comment, but {how} shows this is "
                         f"{officials[official]} {official}; moved to Discussion"})
            moved += 1
            continue
        if not who and voice_name:
            c["speaker"] = voice_name
            c["_info"] = "name matched by voice"
            named += 1
        keep.append(c)
    # One person's comment often comes back as several items (one per point
    # made). Back-to-back items from the same named speaker become one.
    merged = []
    for c in keep:
        prev = merged[-1] if merged else None
        if (prev and c["speaker"] and c["speaker"] == prev["speaker"]
                and c["timestamp_seconds"] - prev["timestamp_seconds"] <= 300):
            prev["summary"] = f"{prev['summary'].rstrip()} {c['summary'].strip()}"
            if c.get("position") != prev.get("position"):
                prev["position"] = "unclear" if "unclear" in (c.get("position"), prev.get("position")) \
                    else prev["position"]
            prev["_merged"] = prev.get("_merged", 1) + 1
            continue
        merged.append(c)
    for c in merged:
        if c.get("_merged"):
            note = f"{c.pop('_merged')} back-to-back items from the same speaker combined"
            c["_info"] = f"{c['_info']}; {note}" if c.get("_info") else note
    summary["public_comments"] = merged
    summary["discussions"].sort(key=lambda d: d["timestamp_seconds"])
    return {"moved": moved, "named": named}


def credit_motions(summary, vid: str) -> int:
    """Once minutes.py has read the official minutes, each vote gets its mover,
    seconder and official result. The minutes say who moved what; this only
    decides which vote each motion in the minutes belongs to: same subject,
    nearest to where the motion was made, and one motion per vote."""
    path = HERE / "minutes" / f"{vid}.json"
    if not path.exists():
        return 0
    from minutes import SYNONYMS
    motions = json.loads(path.read_text()).get("motions", [])

    def words_for(text):
        low = text.lower()
        return {w for key, ws in SYNONYMS.items() if key in low for w in ws}

    agrees = {"passed": {"passed"}, "failed": {"failed"},
              "out of order": {"died for lack of a second", "withdrawn", "unclear"},
              "not recorded": {"passed", "failed", "unclear", "died for lack of a second"}}
    pairs = []
    for di, d in enumerate(summary["decisions"]):
        text = d["motion"].lower()
        for mi, m in enumerate(motions):
            score = sum(w in text for w in words_for(m["topic"]))
            if not score:
                continue
            agree = d["outcome"] in agrees.get(m["result"], set()) or d["outcome"] == "unclear"
            where = m.get("transcript_time")
            distance = abs(d["timestamp_seconds"] - where) if where is not None else 10**6
            pairs.append((not agree, distance, -score, di, mi))
    used_d, used_m, credited = set(), set(), 0
    for _, _, _, di, mi in sorted(pairs):
        if di in used_d or mi in used_m:
            continue
        used_d.add(di), used_m.add(mi)
        d, m = summary["decisions"][di], motions[mi]
        d["moved_by"], d["seconded_by"] = m["moved_by"], m["seconded_by"]
        d["official"] = {"result": m["result"], "count": m["count"], "roll_call": m["roll_call"]}
        credited += 1
    summary["overview_votes"] = votes_line(summary["decisions"])
    return credited


def official_spellings(summary, vid: str, board: str) -> list:
    """Spell names the way the official minutes do. Whisper writes names by
    ear, so a commenter who says "my name is Alexis Budnek" becomes "alexis
    badnick" in the transcript, and the summary copies that. Once minutes.py
    has read the minutes, a name in a public comment is replaced when the
    minutes list someone with the same first name and a last name that sounds
    the same (see minutes.official_spelling). Quotes keep the transcript's
    words: they're checked against it."""
    path = HERE / "minutes" / f"{vid}.json"
    if not path.exists():
        return []
    from minutes import names_in_minutes, official_spelling
    official = names_in_minutes(json.loads(path.read_text())) + list(board_roster(board))
    renames = {}
    for c in summary["public_comments"]:
        better = official_spelling(c.get("speaker", ""), official, first_name_only_ok=True)
        if better:
            renames[c["speaker"]] = better
    if not renames:
        return []

    def fix(text: str) -> str:
        # Full names only: a first name alone ("Rich") is fixed in the speaker
        # field, not in prose, where it could be another word ("Rich Mountain").
        for old, new in renames.items():
            if len(old.split()) < 2:
                continue
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
            old_last, new_last = old.split()[-1], new.split()[-1]
            if old_last != new_last:   # "Ms. Badnick" -> "Ms. Budnek"
                text = re.sub(rf"\b{re.escape(old_last)}\b", new_last, text)
        return text

    for section in ("decisions", "discussions", "public_comments", "upcoming", "unclear_audio"):
        for item in summary[section]:
            for key, value in item.items():
                if isinstance(value, str) and key != "quote" and not key.startswith("_"):
                    item[key] = fix(value)
    summary["overview"] = fix(summary["overview"])
    for c in summary["public_comments"]:
        heard = c.get("speaker", "")
        if heard in renames:
            c["speaker"] = renames[heard]
            note = ("full name from the official minutes" if len(heard.split()) < 2
                    else "name spelled as in the official minutes")
            c["_info"] = f"{c['_info']}; {note}" if c.get("_info") else note
    return sorted(renames.items())


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


NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def check_vote(vote, said: set) -> list:
    """Vote counts and 'unanimous' must actually be said near the vote.
    Numbers count whether Whisper wrote them as digits or words: the same
    tally comes out as "3-2" in one transcript and "three two" in another."""
    notes = []
    said = said | {NUMBER_WORDS[w] for w in said if w in NUMBER_WORDS}
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
            notes = item.pop("_notes", []) + check_item(item, segments, starts)
            item.pop("_idx", None)
            item.pop("_added", None)
            info = item.pop("_info", "")
            if info:
                item["info_note"] = info
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
        info = f" *({item['info_note']})*" if item.get("info_note") else ""
        return info + ("" if item.get("verified", True) else f" ⚠️ *{item['verify_note']}*")

    lines = [f"# {meeting.get('title', path.stem)}", ""]
    if meeting.get("board") or meeting.get("meeting_date"):
        lines += [f"**{meeting.get('board', '')}** · {meeting.get('meeting_date', '')}", ""]
    official = meeting.get("official", {})
    if official:
        # From agendas.py: the city's own record of this meeting.
        links = [f"[Agenda]({official['agenda_url']})" if official.get("agenda_url") else "",
                 f"[Packet]({official['packet_url']})" if official.get("packet_url") else "",
                 f"[Official minutes]({official['minutes_url']})" if official.get("minutes_url")
                 else "Official minutes: not posted yet"]
        lines += ["Official record: " + " · ".join(l for l in links if l)
                  + ". Where this summary and the minutes disagree, the minutes are the record.", ""]
    lines += ["## Overview", "", summary["overview"], "", f"**Votes:** {summary['overview_votes']}"]
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
    prose, removed = strip_vote_talk(
        overview_pass(ai, meeting, topics["part_overviews"], topics["upcoming"]))
    return {
        "overview": prose,
        "overview_votes": votes_line(decisions),     # written by code, not the model
        "overview_removed": removed,                 # sentences dropped for mentioning votes
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
                        help="ollama (free, local), gemini (free tier API) or claude (paid API)")
    parser.add_argument("--tag", help="save as summaries/<id>.<tag>.md so runs can be compared")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore saved AI answers and ask the model again")
    parser.add_argument("--model", help="model name (default depends on backend)")
    parser.add_argument("--speakers", action="store_true",
                        help="show the model who is speaking (off by default: on the Nov 3 test "
                             "it cost the local model 2 of 6 votes)")
    parser.add_argument("--chunk-chars", type=int,
                        help="transcript characters per chunk (default depends on backend)")
    args = parser.parse_args()
    model = args.model or DEFAULTS[args.backend]["model"]
    chunk_chars = args.chunk_chars or DEFAULTS[args.backend]["chunk_chars"]

    load_env()
    key_name = API_KEYS.get(args.backend)
    if key_name and not os.environ.get(key_name):
        print("[!] No API key. Add this line to a .env file in this folder:")
        print(f"    {key_name}=your-key-here")
        return 1

    meetings = json.loads(MEETINGS_FILE.read_text()) if MEETINGS_FILE.exists() else {}
    if args.video_id:
        ids = [args.video_id]
    else:
        ids = [k for k, m in meetings.items() if m.get("status") == "transcribed"]
    if not ids:
        print("Nothing to summarize. Transcribe a meeting first.")
        return 0

    backend = {"ollama": OllamaBackend, "gemini": GeminiBackend,
               "claude": ClaudeBackend}[args.backend](model)
    print(f"Using {args.backend} / {model}")
    SUMMARIES_DIR.mkdir(exist_ok=True)

    for vid in ids:
        transcript_path = TRANSCRIPTS_DIR / f"{vid}.json"
        if not transcript_path.exists():
            print(f"[!] No transcript for {vid}; skipping.")
            continue
        meeting = meetings.get(vid, {"title": vid})
        segments = labeled = json.loads(transcript_path.read_text())["segments"]
        if not args.speakers:
            segments = strip_speakers(segments)
        print(f"\nSummarizing: {meeting.get('title', vid)} ({len(segments)} transcript lines)")

        ai = CachedAI(backend, use_cache=not args.no_cache)
        started = time.time()
        try:
            summary, vote_events = summarize(ai, meeting, segments, chunk_chars)
        except Exception as err:
            print(f"  [!] {err}")
            continue
        elapsed = time.time() - started
        credited = credit_speakers(summary, labeled, meeting.get("board", ""))
        motions_credited = credit_motions(summary, vid)
        for heard, official in official_spellings(summary, vid, meeting.get("board", "")):
            print(f"  Minutes: '{heard}' spelled '{official}', as in the official minutes")
        if motions_credited:
            print(f"  Minutes: credited {motions_credited} vote(s) with mover, seconder and official result")
        if credited["moved"] or credited["named"]:
            print(f"  Speakers: moved {credited['moved']} official's remark(s) out of public comment, "
                  f"named {credited['named']} commenter(s) by voice")

        failures = run_checks(summary, segments)
        items = sum(len(summary[k]) for k in
                    ("decisions", "discussions", "public_comments", "upcoming", "unclear_audio"))
        summary["_meta"] = {
            "video_id": vid, "backend": args.backend, "model": model, "version": 6,
            "speakers": any(s.get("turns") for s in segments),
            "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(elapsed, 1), "ai_calls": ai.calls, "cached_calls": ai.cached,
            "input_tokens": ai.tokens_in, "output_tokens": ai.tokens_out,
            "vote_events_found": vote_events, "items": items, "unverified_items": failures,
        }
        name = f"{vid}.{args.tag}" if args.tag else vid
        json_path = SUMMARIES_DIR / f"{name}.json"
        md_path = SUMMARIES_DIR / f"{name}.md"
        json_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_markdown(md_path, meeting, summary)

        print(f"  {len(summary['decisions'])} decisions (from {vote_events} vote events), "
              f"{len(summary['discussions'])} discussion topics, "
              f"{len(summary['public_comments'])} public comments")
        print(f"  Took {hms(elapsed)}; {ai.calls} AI calls ({ai.cached} reused from cache); "
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
