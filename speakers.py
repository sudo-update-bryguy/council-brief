#!/usr/bin/env python3
"""
speakers.py: step 2b of the pipeline (between transcribe and summarize).

Works out who is speaking on every line of a transcript:
  1. Diarization: pyannote (speaker-diarization-community-1, on your GPU)
     splits the recording into turns: "Speaker 1 spoke from 0:32 to 0:41".
     It can tell voices apart but doesn't know anyone's name.
  2. Alignment: every word in the transcript is assigned to the speaker
     talking at that moment. Whisper's lines are kept; a line where the
     speaker changes gets "turns". One-word flickers are smoothed out.
  3. Naming: the AI matches voices to people, using only evidence in the
     transcript ("my name is...", "Thank you, Eric", who called the meeting
     to order) and the board's public roster (roster.json).
  4. Checks: code confirms every name against its evidence. A name the
     evidence doesn't support is removed, and that voice stays "Speaker 3".
     Code also adds names the AI missed, with three narrow rules: a first
     name is expanded if that voice said "my name is <full name>", the voice
     that calls the meeting to order is the roster's presiding officer, and a
     voice that takes the floor right after being called on by name gets
     that name.

Usage:
  python speakers.py --video-id su1pgPIVpbM
  python speakers.py --video-id su1pgPIVpbM --backend gemini --model gemini-3.5-flash-lite

Needs HF_TOKEN in .env (a free Hugging Face token, after accepting the terms
at huggingface.co/pyannote/speaker-diarization-community-1), and the
recording in recordings/<video_id>.* (the same file you transcribed).

Output: updates transcripts/<id>.json (speaker on every line) and writes
transcripts/<id>.speakers.txt (the transcript with names).
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import summarize as S   # reuse the AI backends, cache, and helpers

HERE = Path(__file__).resolve().parent
ROSTER_FILE = HERE / "roster.json"
RECORDINGS_DIR = HERE / "recordings"
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".flac", ".ogg"}

ROLES = ["chair", "board member", "staff", "presenter", "applicant",
         "member of the public", "unknown"]


# ---------- 1. diarization ----------

def diarize(media: Path, hf_token: str):
    """Return a list of (start, end, label) turns in recording time."""
    import torch
    from faster_whisper import decode_audio
    from pyannote.audio import Pipeline

    try:
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1",
                                            token=hf_token)
    except TypeError:   # older pyannote versions call it use_auth_token
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1",
                                            use_auth_token=hf_token)
    if pipeline is None:
        raise RuntimeError("couldn't load pyannote. Did you accept the terms at "
                           "huggingface.co/pyannote/speaker-diarization-community-1?")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline.to(torch.device(device))
    print(f"  Diarizing on {device.upper()} (this is the slow part)...", flush=True)

    # Decode with the same library Whisper uses, so any format Whisper reads works here.
    audio = decode_audio(str(media), sampling_rate=16000)
    output = pipeline({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": 16000})
    # Newer pyannote returns an object; "exclusive" means one speaker at a time,
    # which lines up cleanly with a transcript.
    annotation = (getattr(output, "exclusive_speaker_diarization", None)
                  or getattr(output, "speaker_diarization", None) or output)
    return [(turn.start, turn.end, label)
            for turn, _, label in annotation.itertracks(yield_label=True)]


# ---------- 2. alignment ----------

def speaker_at(t: float, turns) -> str | None:
    best, best_gap = None, 1.0   # allow up to 1 s of slack at turn edges
    for start, end, label in turns:
        if start <= t <= end:
            return label
        gap = min(abs(t - start), abs(t - end))
        if gap < best_gap:
            best, best_gap = label, gap
    return best


# Short answers that matter in a meeting. These keep their own speaker even
# when they're a single word; any other one-word "turn" is usually the
# diarizer flickering between voices, so it joins the words around it.
SHORT_ANSWERS = {"second", "seconded", "aye", "ayes", "nay", "no", "yes", "opposed",
                 "abstain", "moved", "motion", "here", "present"}


def word_runs(words, turns):
    """Consecutive words by the same speaker -> [[label, [words...]], ...]"""
    runs = []
    for w in words:
        label = speaker_at((w["start"] + w["end"]) / 2, turns) or (runs[-1][0] if runs else None)
        if runs and runs[-1][0] == label:
            runs[-1][1].append(w)
        else:
            runs.append([label, [w]])
    return runs


def smooth(runs):
    """Fold one-word flickers into the previous run (unless it's 'second', 'aye'...)."""
    out = []
    for label, ws in runs:
        token = S.words(ws[0]["word"])
        flicker = len(ws) == 1 and not (token and token[0] in SHORT_ANSWERS)
        if out and (flicker or out[-1][0] == label):
            out[-1][1].extend(ws)
        else:
            out.append([label, list(ws)])
    # A flicker at the very start of a line joins the run after it.
    if len(out) > 1 and len(out[0][1]) == 1 and not (
            S.words(out[0][1][0]["word"]) and S.words(out[0][1][0]["word"])[0] in SHORT_ANSWERS):
        out[1][1][:0] = out[0][1]
        out.pop(0)
    return out


def align(segments, turns, offset: float):
    """Give every Whisper line a speaker, plus "turns" when the speaker changes
    mid-line. Whisper's lines stay as they are, so the summarizer sees the same
    lines (and line numbers) with or without speakers.
    Transcript times are recording time minus the OBS lead-in (offset)."""
    turns = [(s - offset, e - offset, lab) for s, e, lab in turns]
    out = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:   # older transcript without word timings: best overlap for the line
            overlap = defaultdict(float)
            for s, e, lab in turns:
                overlap[lab] += max(0.0, min(e, seg["end"]) - max(s, seg["start"]))
            label = max(overlap, key=overlap.get) if any(overlap.values()) else None
            out.append({**seg, "speaker": label, "turns": [
                {"speaker": label, "start": seg["start"], "end": seg["end"], "text": seg["text"]}]})
            continue
        runs = smooth(word_runs(words, turns))
        line_turns = [{"speaker": lab, "start": round(ws[0]["start"], 2),
                       "end": round(ws[-1]["end"], 2),
                       "text": "".join(w["word"] for w in ws).strip()} for lab, ws in runs]
        talk = defaultdict(float)
        for t in line_turns:
            talk[t["speaker"]] += t["end"] - t["start"]
        out.append({**seg, "speaker": max(talk, key=talk.get), "turns": line_turns})
    return out


def turn_lines(segments):
    """Flatten to one entry per turn (what people read, and what naming uses)."""
    return [t for seg in segments for t in seg["turns"] if t["text"]]


def relabel(segments):
    """pyannote's SPEAKER_07 -> 'Speaker 1', 'Speaker 2'... in order of first appearance."""
    names = {}
    for seg in segments:
        for item in seg["turns"] + [seg]:
            raw = item.get("speaker")
            if raw is not None and raw not in names and not str(raw).startswith("Speaker "):
                names[raw] = f"Speaker {len(names) + 1}"
    for seg in segments:
        for item in seg["turns"] + [seg]:
            item["speaker"] = names.get(item.get("speaker"), item.get("speaker") or "Speaker ?")
    return segments


# ---------- 3. naming ----------

NAMING_RULES = """You identify the speakers in a transcript of a public local government \
meeting in Blue Ridge, Georgia. Voices were separated automatically and labeled \
"Speaker 1", "Speaker 2", and so on. Match labels to people ONLY with evidence from the \
transcript:
- Self-introduction: "my name is ...", "I'm ... with ...". The speaker of that line is that person.
- Being called on: someone says "Thank you, Eric" or "Jennifer, do you want to go over..." \
and a different speaker answers right after. The answering speaker is that person.
- Presiding: the speaker who calls the meeting to order and announces agenda items is the \
chair. The roster says who normally presides.
Rules:
- "evidence_quote" must be copied word for word from the transcript (under 25 words) and \
must contain the person's name, unless the role is "chair" and the quote is the chair \
calling the meeting to order or announcing an item.
- If the evidence isn't clear, leave "name" empty. A wrong name is much worse than no name.
- People not on the roster can only be named if they introduce themselves.
- role: chair, board member, staff, presenter, applicant, member of the public, or unknown. \
Use the roster titles for roles. Anyone who comes forward during public comment or to speak \
for or against an application is "member of the public" or "applicant"."""

NAMING_SCHEMA = S.obj({"speakers": S.array_of({
    "speaker": {"type": "string", "description": "label, e.g. 'Speaker 3'"},
    "name": {"type": "string", "description": "full name, or empty if not clear"},
    "role": {"type": "string", "enum": ROLES},
    "evidence_quote": {"type": "string"},
})})

NAME_CLUES = re.compile(r"\b(my name is|i'?m [a-z]+ (with|from)|thank you,? [A-Z]|recognize|"
                        r"call (our|this|the) .* to order|to order)\b", re.IGNORECASE)


def load_roster(board: str) -> dict:
    roster = json.loads(ROSTER_FILE.read_text()) if ROSTER_FILE.exists() else {}
    entry = roster.get(board, {})
    if "same_as" in entry:
        entry = roster.get(entry["same_as"], {})
    return entry


def naming_prompt(meeting, lines, roster) -> str:
    talk = defaultdict(float)
    for line in lines:
        talk[line["speaker"]] += line["end"] - line["start"]
    people = roster.get("people", [])
    name_words = {part.lower() for p in people for part in p["name"].split() if len(part) > 2}

    def mentions_name(text):
        return any(w in name_words for w in S.words(text))

    # Lines worth showing: name clues, lines mentioning roster names, the line
    # after each of those (who answered), and a few sample lines per speaker.
    keep = set()
    for i, line in enumerate(lines):
        if NAME_CLUES.search(line["text"]) or mentions_name(line["text"]):
            keep.update({i, i + 1})
    samples = defaultdict(list)
    for i, line in enumerate(lines):
        if len(samples[line["speaker"]]) < 3 and len(line["text"].split()) >= 8:
            samples[line["speaker"]].append(i)
    for idx in samples.values():
        keep.update(idx)
    shown = [lines[i] for i in sorted(k for k in keep if k < len(lines))][:160]

    roster_text = "\n".join(f"- {p['name']} ({p['title']})" for p in people) or "- (no roster)"
    speakers_text = "\n".join(f"- {sp}: talks {S.hms(sec)}"
                              for sp, sec in sorted(talk.items(), key=lambda x: -x[1]))
    transcript = "\n".join(f"[{S.hms(l['start'])}] {l['speaker']}: {l['text']}" for l in shown)
    return (f"{S.meeting_header(meeting)}\nUsually presides: {roster.get('presiding', 'unknown')}\n"
            f"Roster:\n{roster_text}\n\nSpeakers found (by talk time):\n{speakers_text}\n\n"
            f"Relevant transcript lines (excerpts, in order):\n{transcript}")


# "my name is Richard Chism", "I'm Richard Chism", "this is Richard Chism".
SELF_INTRO = re.compile(r"\b(?:[Nn]ame is|I'?m|I am|[Tt]his is) ([A-Za-z]+)(?: ([A-Z][a-z]+))?")


def check_names(proposals, lines, roster):
    """Keep a name only when the transcript supports it."""
    all_words = S.words(" ".join(l["text"] for l in lines))
    labels = {l["speaker"] for l in lines}
    roster_names = {p["name"]: p["title"] for p in roster.get("people", [])}
    presiding = roster.get("presiding", "")
    result = {}
    for p in proposals:
        label = p.get("speaker", "")
        if label not in labels or label in result:
            continue
        name, role, quote = p.get("name", "").strip(), p.get("role", "unknown"), p.get("evidence_quote", "")
        notes = []
        quote_ok = bool(quote) and S.quote_found(S.words(quote), all_words)
        if name:
            # "Rich" counts as mentioned by "Richard" (and "Jen" by "Jennifer").
            name_in_quote = any(w.startswith(part.lower()) for w in S.words(quote)
                                for part in name.split() if len(part) > 2)
            chair_ok = (role == "chair" and name == presiding
                        and re.search(r"order|item|recognize|move on", quote, re.I))
            if not quote_ok:
                notes.append(f"evidence for '{name}' isn't in the transcript; name removed")
                name = ""
            elif not (name_in_quote or chair_ok):
                notes.append(f"evidence doesn't mention '{name}'; name removed")
                name = ""
            elif name not in roster_names and not SELF_INTRO.search(quote):
                notes.append(f"'{name}' isn't on the roster and didn't introduce themselves; "
                             f"name removed")
                name = ""
        if name in roster_names and role in ("unknown", "member of the public"):
            role = "chair" if name == presiding else (
                "staff" if any(t in roster_names[name].lower()
                               for t in ("clerk", "attorney", "consultant")) else "board member")
        result[label] = {"name": name, "role": role, "evidence": quote if quote_ok else "",
                         "title": roster_names.get(name, ""), "notes": notes}
    for label in labels - result.keys():
        result[label] = {"name": "", "role": "unknown", "evidence": "", "title": "", "notes": []}
    return result


def first_name_match(given: str, full: str) -> bool:
    """'Rich' matches 'Richard Chism'; 'Jen' matches 'Jennifer Bates'."""
    a, b = given.lower(), full.split()[0].lower()
    return len(a) >= 3 and (a.startswith(b) or b.startswith(a))


def expand_names(speakers, lines):
    """The AI sometimes gives a first name only ("Rich"). If that same voice
    said "my name is Richard Chism", use the full name."""
    for label, info in speakers.items():
        if not info["name"] or len(info["name"].split()) > 1:
            continue
        for line in lines:
            if line["speaker"] != label:
                continue
            # The surname must be capitalized, so "I'm rich and..." can't match.
            m = next((m for m in SELF_INTRO.finditer(line["text"]) if m.group(2)
                      and first_name_match(info["name"], m.group(1))), None)
            if m:
                full = f"{m.group(1).title()} {m.group(2)}"
                info["notes"].append(f"'{info['name']}' expanded to '{full}' "
                                     f"(they said: \"{m.group(0)}\")")
                info["name"], info["evidence"] = full, line["text"]
                break


ORDER_CUE = re.compile(r"\b(call|bring|calling|bringing)\b.{0,60}\bto order\b", re.IGNORECASE)


def presiding_officer(speakers, lines, roster, first_minutes=5):
    """Code-only rule: the voice that calls the meeting to order in the first
    few minutes is the roster's presiding officer, unless that name is
    already on another voice."""
    presiding = roster.get("presiding", "")
    if not presiding or any(i["name"] == presiding for i in speakers.values()):
        return
    line = next((l for l in lines if l["start"] <= first_minutes * 60
                 and ORDER_CUE.search(l["text"])), None)
    if not line or speakers[line["speaker"]]["name"]:
        return
    title = next((p["title"] for p in roster.get("people", []) if p["name"] == presiding), "")
    speakers[line["speaker"]].update({"name": presiding, "role": "chair",
                                      "evidence": line["text"], "title": title})
    speakers[line["speaker"]]["notes"].append(
        f"named by code: called the meeting to order (\"{line['text'][:60]}\")")


INVITE_CUES = re.compile(r"\b(do you want|would you|could you|can you|go ahead|you have the "
                         r"floor|come (up|on up|forward)|please|your report|turn it over|"
                         r"recognize|to present|present the)\b|\?", re.IGNORECASE)


def called_on(speakers, lines, roster, window=15.0, min_talk=20.0):
    """Code-only rule for the most common hand-off in these meetings: the chair
    says "Jennifer, do you want to go over the details", and within a few
    seconds a new voice takes the floor and keeps talking. If that voice is
    unnamed, and the name is on the roster and not taken by another voice,
    it's that person. "Thank you, Jennifer" (said after someone finishes)
    doesn't count, and any conflict leaves the voice unnamed."""
    people = roster.get("people", [])
    taken = {info["name"] for info in speakers.values() if info["name"]}
    found = defaultdict(set)   # label -> {(roster name, evidence line)}
    for i, line in enumerate(lines):
        said = set(S.words(line["text"]))
        named = [p for p in people if p["name"] not in taken
                 and p["name"].split()[0].lower() in said]
        if not named or "thank" in said or not INVITE_CUES.search(line["text"]):
            continue
        # The first different voice to speak within `window` seconds...
        nxt = next((l for l in lines[i + 1:] if l["speaker"] != line["speaker"]), None)
        if not nxt or nxt["start"] > line["end"] + window:
            continue
        # ...and it has to hold the floor (talk at least min_talk seconds in the next minute).
        talk = sum(l["end"] - l["start"] for l in lines[i + 1:]
                   if l["speaker"] == nxt["speaker"] and l["start"] <= nxt["start"] + 60)
        if talk >= min_talk and not speakers[nxt["speaker"]]["name"]:
            for p in named:
                found[nxt["speaker"]].add((p["name"], line["text"]))

    claims = defaultdict(set)   # roster name -> labels it was matched to
    for label, pairs in found.items():
        for name, _ in pairs:
            claims[name].add(label)
    for label, pairs in found.items():
        names = {n for n, _ in pairs}
        if len(names) > 1:
            speakers[label]["notes"].append(f"called on as more than one person "
                                            f"({', '.join(sorted(names))}); left unnamed")
            continue
        name = names.pop()
        if len(claims[name]) > 1:
            speakers[label]["notes"].append(f"'{name}' matched more than one voice; left unnamed")
            continue
        evidence = next(t for n, t in pairs if n == name)
        title = next(p["title"] for p in people if p["name"] == name)
        role = "chair" if name == roster.get("presiding") else (
            "staff" if any(t in title.lower() for t in
                           ("clerk", "attorney", "consultant", "manager", "director", "planner"))
            else "board member")
        speakers[label].update({"name": name, "role": role, "evidence": evidence, "title": title})
        speakers[label]["notes"].append(f"named by code: called on by name, then took the floor "
                                        f"(\"{evidence[:70]}\")")


# ---------- output ----------

def display_name(label, info):
    if info["name"]:
        return f"{info['name']}" + (f" ({info['title']})" if info["title"] else
                                     f" ({info['role']})" if info["role"] != "unknown" else "")
    return label + (f" ({info['role']})" if info["role"] != "unknown" else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Identify who is speaking in a transcript.")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--recording", type=Path, help="recording file (default: recordings/<id>.*)")
    parser.add_argument("--backend", choices=S.DEFAULTS, default="ollama")
    parser.add_argument("--model", help="model for naming (default depends on backend)")
    args = parser.parse_args()

    S.load_env()
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("[!] Add HF_TOKEN=your-token to .env (see the top of this file).")
        return 1
    key_name = S.API_KEYS.get(args.backend)
    if key_name and not os.environ.get(key_name):
        print(f"[!] Add {key_name}=your-key to .env")
        return 1

    transcript_path = S.TRANSCRIPTS_DIR / f"{args.video_id}.json"
    if not transcript_path.exists():
        print(f"[!] No transcript at {transcript_path}. Run transcribe.py first.")
        return 1
    media = args.recording or next((f for f in sorted(RECORDINGS_DIR.glob(f"{args.video_id}.*"))
                                    if f.suffix.lower() in MEDIA_EXTENSIONS), None)
    if not media or not media.exists():
        print(f"[!] No recording found for {args.video_id} in recordings/.")
        return 1

    data = json.loads(transcript_path.read_text())
    if "diarization" in data and "whisper_segments" not in data:
        print("[!] This transcript was split by an older speakers.py. Re-run transcribe.py")
        print("    for this meeting first, then speakers.py.")
        return 1
    meetings = json.loads(S.MEETINGS_FILE.read_text()) if S.MEETINGS_FILE.exists() else {}
    meeting = meetings.get(args.video_id, {"title": args.video_id})
    roster = load_roster(meeting.get("board", ""))
    if not any(s.get("words") for s in data["segments"]):
        print("  Note: this transcript has no word timings, so lines can't be split between")
        print("  speakers. Re-run transcribe.py for the best results.")

    print(f"Speakers for: {meeting.get('title', args.video_id)}")
    started = time.time()
    # Keep Whisper's original lines so speakers.py can be re-run safely.
    whisper = data.setdefault("whisper_segments", data["segments"])
    turns = diarize(media, hf_token)
    segments = relabel(align(whisper, turns, data.get("offset_seconds", 0.0)))
    lines = turn_lines(segments)
    print(f"  {len({l['speaker'] for l in lines})} voices, {len(lines)} turns on "
          f"{len(segments)} lines (diarized in {S.hms(time.time() - started)})")

    model = args.model or S.DEFAULTS[args.backend]["model"]
    backend = {"ollama": S.OllamaBackend, "gemini": S.GeminiBackend,
               "claude": S.ClaudeBackend}[args.backend](model)
    print(f"  Naming voices with {args.backend} / {model}...", flush=True)
    proposals = S.CachedAI(backend).complete(
        NAMING_RULES, naming_prompt(meeting, lines, roster), NAMING_SCHEMA).get("speakers", [])
    speakers = check_names(proposals, lines, roster)
    expand_names(speakers, lines)
    presiding_officer(speakers, lines, roster)
    called_on(speakers, lines, roster)

    for seg in segments:
        for item in seg["turns"] + [seg]:
            info = speakers[item["speaker"]]
            item["speaker_name"] = info["name"]
            item["speaker_role"] = info["role"]
            item["speaker_display"] = display_name(item["speaker"], info)

    data.update({"segments": segments, "speakers": speakers, "diarization": {
        "model": "pyannote/speaker-diarization-community-1", "naming_model": model,
        "voices": len(speakers), "turns": len(lines)}})
    transcript_path.write_text(json.dumps(data, indent=2) + "\n")
    txt = S.TRANSCRIPTS_DIR / f"{args.video_id}.speakers.txt"
    txt.write_text("".join(f"[{S.hms(l['start'])}] {l['speaker_display']}: {l['text']}\n"
                           for l in lines))

    talk = defaultdict(float)
    for line in lines:
        talk[line["speaker"]] += line["end"] - line["start"]
    print("\n  Voice        Talk time  Identified as")
    for label, sec in sorted(talk.items(), key=lambda x: -x[1]):
        info = speakers[label]
        shown = display_name(label, info) if info["name"] else f"(not identified, role: {info['role']})"
        print(f"  {label:<12} {S.hms(sec):<10} {shown}")
        for note in info["notes"]:
            print(f"  {'':<12} {'':<10} ⚠️  {note}")
    print(f"\n  Saved {txt.relative_to(HERE)} and updated {transcript_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
