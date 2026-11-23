#!/usr/bin/env python3
"""
minutes.py: use a meeting's official minutes once they're posted.

The minutes are the official record, and they say things the recording can't
prove on its own: who made and seconded each motion, how each member voted,
and who led the prayer. This script:

  1. downloads the minutes PDF (the link comes from agendas.py) and reads it
     into minutes/<id>.json: motions, movers, seconders, results, roll calls;
  2. names voices from it. "Motion to rezone to CBD ... made by William
     Whaley" + the moment in the transcript where a voice says "I'd like to make
     a motion" about the CBD = that voice is Whaley. A voice is named only when
     every piece of evidence for it agrees, the name isn't already on another
     voice, and speakers.py didn't name it something else;
  3. leaves the motion credits in minutes/<id>.json, where summarize.py picks
     them up ("Moved by ..., seconded by ...", and the official result).

"Second" never names a voice: it's often one word, which is where voice
matching is weakest (on the Nov 3 test, a "Second." was credited to the same
voice that made the motion). Seconders come from the minutes only.

Usage (after speakers.py, once agendas.py reports the minutes are posted):
  python minutes.py --video-id su1pgPIVpbM
  python minutes.py --video-id su1pgPIVpbM --pdf saved_minutes.pdf
Then re-run summarize.py for that meeting (its AI answers are cached, so it
takes seconds) and site.py.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
TRANSCRIPTS_DIR = HERE / "transcripts"
MINUTES_DIR = HERE / "minutes"
ROSTER_FILE = HERE / "roster.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) council-brief/0.1"

# Words that tie a motion in the minutes to the same motion in the transcript.
# Zoning districts are spoken differently from how they're written.
SYNONYMS = {
    "cbd": ["cbd", "central business"],
    "c-2": ["c2", "c-2", "c 2", "community commercial"],
    "c2": ["c2", "c-2", "c 2", "community commercial"],
    "m-1": ["m1", "m-1", "manufacturing"],
    "conditional": ["conditional use", "permit"],
    "minutes": ["minutes"],
    "agenda": ["agenda"],
    "extend": ["extend", "more time", "additional time", "proceed"],
}
NAME = r"([A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,2})"


# ---------- 1. reading the minutes ----------

def pdf_text(path: Path) -> str:
    """pdftotext keeps words intact; pypdf sometimes splits them ("Commer cial")."""
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    import pypdf
    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages)


def download(url: str, path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data.startswith(b"%PDF"):
        raise ValueError("the minutes link didn't return a PDF")
    path.write_bytes(data)


def known_people(board: str) -> list:
    roster = json.loads(ROSTER_FILE.read_text()) if ROSTER_FILE.exists() else {}
    entry = roster.get(board, {})
    entry = roster.get(entry["same_as"], {}) if "same_as" in entry else entry
    return entry.get("people", [])


def resolve(raw: str, people: list) -> str:
    """'Bill Barber' -> 'Bill Barber'; 'Panter' -> 'Mike Panter'; '"Bubba" Gibbs' -> 'Bubba Gibbs'.
    Compares without spaces, because PDF text sometimes splits words."""
    squash = lambda s: re.sub(r"[^a-z]", "", s.lower())
    raw = raw.strip()
    for p in people:
        if squash(p["name"]) == squash(raw):
            return p["name"]
    last = squash(raw.split()[-1]) if raw.split() else ""
    hits = [p["name"] for p in people if squash(p["name"].split()[-1]) == last]
    return hits[0] if len(hits) == 1 else raw


def parse_minutes(text: str, people: list) -> dict:
    flat = " ".join(text.split())
    # Cut the text into one chunk per motion.
    starts = [m.start() for m in re.finditer(r"(?<!\()\bMotion\b(?! to Amend Agenda)", flat)]
    motions = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(flat)
        chunk = re.split(r"\s\d+\.\s+[A-Z]", flat[start:end])[0]   # stop at the next agenda item
        mover = re.search(r"made by " + NAME, chunk)
        if not mover:
            continue   # "Consideration of a Motion to ..." is an agenda heading, not a motion
        seconder = re.search(r"seconded by " + NAME, chunk)
        # Roll call: "Panter- Yes, Barber- No". Only names on the board count, so
        # "Speaking in Opposition- No One" isn't read as a vote.
        members = {p["name"] for p in people}
        roll = {name: v.lower() for name, v in
                ((resolve(n, people), v) for n, v in re.findall(r"([A-Z][a-z]+)\s*[-–]\s*(Yes|No)\b", chunk))
                if name in members}
        if re.search(r"out of order", chunk, re.I):
            result, count = "out of order", ""
        elif roll:
            yes = sum(v == "yes" for v in roll.values())
            result = "passed" if yes > len(roll) - yes else "failed"
            count = f"{yes}-{len(roll) - yes}"
        elif re.search(r"all in favor", chunk, re.I):
            result, count = "passed", "all in favor"   # the minutes' own words
        else:
            result, count = "not recorded", ""
        topic = re.sub(r"\s*(was )?made by .*$", "", chunk).strip()
        if topic in ("Motion", "Motion was"):   # "Motion was made by X to extend speakers time"
            topic = "Motion " + re.sub(r"^.*?made by " + NAME + r"\s*", "", chunk).split(",")[0]
        motions.append({
            "topic": topic, "moved_by": resolve(mover.group(1), people),
            "seconded_by": resolve(seconder.group(1), people) if seconder else "",
            "result": result, "count": count, "roll_call": roll, "text": chunk.strip()})
    prayer = re.search(r"Prayer.{0,60}?Led by " + NAME, flat)
    order = re.search(r"Call(?:ed)? (?:the )?Meeting to Order.{0,30}? by " + NAME, flat, re.I)
    return {
        "motions": motions,
        "prayer_by": resolve(prayer.group(1), people) if prayer else "",
        "called_to_order_by": resolve(order.group(1), people) if order else "",
        "speaking": re.findall(r"Speaking in (Favor|Opposition)\s*[-–]\s*" + NAME, flat),
    }


# ---------- 2. lining the minutes up with the transcript ----------

MAKE = re.compile(r"\b(i(?:'d| would)? like to make a motion|i(?:'ll)? make a motion|"
                  r"i(?:'d)? move (?:that|to|we)|i so move|so moved)\b", re.I)
ASK = re.compile(r"\b(?:can|could|may) (?:i|we) (?:get|have|make)|anyone want|what'?s (?:your|the) motion|"
                 r"is there a motion|do (?:i|we) have a|need a motion", re.I)


def keywords(topic: str) -> list:
    low = topic.lower()
    found = [w for key, words in SYNONYMS.items() if key in low for w in words]
    return sorted(set(found))


def turns_of(segments):
    return [t for s in segments for t in (s.get("turns") or []) if t.get("text")]


def motion_moments(turns):
    """Moments where a voice makes a motion (not where the chair asks for one).

    Searches each turn together with the next one, because voice matching can
    split a sentence between two voices: on the Nov 10 test, "...how do we
    handle that i make" went to the public speaker and "a motion to extend your
    time" to the council member who made it. The motion is credited to the
    voice that says the word "motion" (or "move"): that voice states it."""
    found = []
    for i, t in enumerate(turns):
        nxt = turns[i + 1] if i + 1 < len(turns) and turns[i + 1]["start"] - t["end"] <= 2 else None
        joined = t["text"] + (" " + nxt["text"] if nxt else "")
        m = MAKE.search(joined)
        if not m:
            continue
        key = re.search(r"motion|move", joined[m.start():m.end()], re.I)
        owner = t if m.start() + key.start() < len(t["text"]) else nxt
        if ASK.search(owner["text"]) or any(owner is f for f in found):
            continue
        found.append(owner)
    return found


def context(turns, t: float, before=20, after=45) -> str:
    return " ".join(x["text"] for x in turns if t - before <= x["start"] <= t + after).lower()


def align(motions, turns):
    """Match each motion in the minutes to the transcript moment where it was
    made: same subject, and in the same order as the minutes."""
    moments = motion_moments(turns)
    after, pairs = -1.0, []
    for m in motions:
        words = keywords(m["topic"])
        scored = [(sum(w in context(turns, x["start"]) for w in words), x) for x in moments
                  if x["start"] > after]
        scored = [(s, x) for s, x in scored if s > 0]
        if not scored:
            pairs.append((m, None))
            continue
        best = max(s for s, _ in scored)
        moment = min((x for s, x in scored if s == best), key=lambda x: x["start"])
        pairs.append((m, moment))
        after = moment["start"]
    return pairs


def prayer_voice(turns):
    """The voice that takes the floor after the chair asks for the prayer."""
    for i, t in enumerate(turns):
        if re.search(r"\bpray", t["text"], re.I) and t["start"] < 600:
            talk = defaultdict(float)
            for x in turns[i + 1:]:
                if x["start"] > t["end"] + 45:
                    break
                if x["speaker"] != t["speaker"]:
                    talk[x["speaker"]] += x["end"] - x["start"]
            if talk:
                voice = max(talk, key=talk.get)
                return (voice, t) if talk[voice] >= 10 else (None, t)
    return None, None


def name_voices(data, parsed, people):
    """Gather every piece of evidence, then name only voices where it all agrees."""
    turns = turns_of(data["segments"])
    evidence = defaultdict(list)   # voice -> [(name, why)]
    pairs = align(parsed["motions"], turns)
    for m, moment in pairs:
        m["transcript_time"] = int(moment["start"]) if moment else None
        if moment:
            evidence[moment["speaker"]].append(
                (m["moved_by"], f"made the motion the minutes credit to {m['moved_by']} "
                                f"(\"{moment['text'][:60]}\", {int(moment['start']) // 60}:{int(moment['start']) % 60:02d})"))
    if parsed["prayer_by"]:
        voice, ask = prayer_voice(turns)
        if voice:
            evidence[voice].append((parsed["prayer_by"], f"led the prayer, which the minutes credit to "
                                                         f"{parsed['prayer_by']}"))

    speakers = data.get("speakers", {})
    claims = defaultdict(set)
    for voice, items in evidence.items():
        for name, _ in items:
            claims[name].add(voice)
    titles = {p["name"]: p["title"] for p in people}
    report = []
    for voice, items in evidence.items():
        names = {n for n, _ in items}
        current = speakers.get(voice, {}).get("name", "")
        why = "; ".join(w for _, w in items)
        if len(names) > 1:
            report.append((voice, "", f"evidence disagrees ({', '.join(sorted(names))}); left as is"))
        elif len(claims[next(iter(names))]) > 1:
            report.append((voice, "", f"{next(iter(names))} matched more than one voice; left as is"))
        elif current and current != next(iter(names)):
            report.append((voice, current, f"minutes suggest {next(iter(names))}, but speakers.py named "
                                           f"this voice {current}; left as is"))
        else:
            name = next(iter(names))
            info = speakers.setdefault(voice, {"name": "", "role": "unknown", "notes": []})
            if current != name:
                info.update({"name": name, "title": titles.get(name, ""),
                             "role": "chair" if info.get("role") == "chair" else
                                     ("staff" if any(k in titles.get(name, "").lower()
                                                     for k in ("clerk", "attorney", "consultant")) else "board member"),
                             "evidence": why})
                info.setdefault("notes", []).append(f"named from the official minutes: {why}")
            report.append((voice, name, why + (" (already named)" if current == name else "")))
    return pairs, report


def apply_names(data):
    """Write the voices' names onto every line, the same way speakers.py does."""
    speakers = data.get("speakers", {})
    for seg in data["segments"]:
        for item in (seg.get("turns") or []) + [seg]:
            info = speakers.get(item.get("speaker"), {})
            if info.get("name"):
                item["speaker_name"] = info["name"]
                item["speaker_role"] = info.get("role", "")
                title = info.get("title") or info.get("role", "")
                item["speaker_display"] = f"{info['name']} ({title})" if title else info["name"]


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Use a meeting's official minutes.")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--pdf", type=Path, help="a minutes PDF you saved, instead of downloading it")
    args = parser.parse_args()
    vid = args.video_id

    meetings = json.loads(MEETINGS_FILE.read_text()) if MEETINGS_FILE.exists() else {}
    meeting = meetings.get(vid, {})
    MINUTES_DIR.mkdir(exist_ok=True)
    pdf = MINUTES_DIR / f"{vid}.pdf"
    if args.pdf:
        shutil.copyfile(args.pdf, pdf)
    elif not pdf.exists():
        url = meeting.get("official", {}).get("minutes_url")
        if not url:
            print("[!] No minutes link for this meeting yet. Run agendas.py, or pass --pdf.")
            return 1
        try:
            download(url, pdf)
        except Exception as err:
            print(f"[!] Couldn't download the minutes ({err}). Save the PDF from the Agenda Center")
            print("    in your browser and run again with --pdf <file>.")
            return 1

    people = known_people(meeting.get("board", ""))
    parsed = parse_minutes(pdf_text(pdf), people)
    print(f"Minutes for: {meeting.get('title', vid)}")
    print(f"  {len(parsed['motions'])} motion(s); prayer led by {parsed['prayer_by'] or '(not listed)'}")

    transcript_path = TRANSCRIPTS_DIR / f"{vid}.json"
    report, labeled = [], False
    if transcript_path.exists():
        data = json.loads(transcript_path.read_text())
        labeled = any(s.get("turns") for s in data["segments"])
        if labeled:
            pairs, report = name_voices(data, parsed, people)
            apply_names(data)
            transcript_path.write_text(json.dumps(data, indent=1) + "\n")
    if not labeled:
        print("  (No voice labels in the transcript; run speakers.py first to name voices.)")

    print("\n  Motion (from the minutes)                        Moved by          Seconded by       "
          "Result        Found at")
    for m in parsed["motions"]:
        t = m.get("transcript_time")
        at = f"{t // 60}:{t % 60:02d}" if t is not None else "-"
        print(f"  {m['topic'][:48]:<48} {m['moved_by'][:17]:<17} {m['seconded_by'][:17]:<17} "
              f"{(m['result'] + ' ' + m['count']).strip()[:13]:<13} {at}")
    if report:
        print("\n  Voices named from the minutes:")
        for voice, name, why in report:
            print(f"    {voice:<11} {name or '(unchanged)':<17} {why}")

    out = MINUTES_DIR / f"{vid}.json"
    out.write_text(json.dumps({"video_id": vid, "source": meeting.get("official", {}).get("minutes_url", ""),
                               **parsed}, indent=2) + "\n")
    print(f"\nSaved {out.relative_to(HERE)}. Next: python summarize.py --video-id {vid} && python site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
