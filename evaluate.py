#!/usr/bin/env python3
"""
evaluate.py: score meeting summaries against a hand-checked answer key.

Usage:
  python evaluate.py summaries/su1pgPIVpbM.json summaries/su1pgPIVpbM.gemini.json

Each summary's video ID is read from the file name (the part before the first
dot), and the answer key is evals/<video_id>.json. Results print as a table
and are appended to evals/history.jsonl so you can track improvement over time.

What gets scored:
  votes found        each real vote appears in Decisions
  outcome correct    passed / failed / died for lack of a second
  confidently wrong  a wrong outcome stated as fact (an honest "unclear" doesn't count),
                     including an extra item that names a real vote with the wrong result
  count = official   vote counts match the official minutes
  count = said       vote counts match what was said aloud (can differ from the record)
  link within 30s    the timestamp lands on the vote
  extra decisions    items in Decisions that aren't real votes
  public comments    entries that aren't one of the real public speakers
  next steps         mentions what happens next (e.g. goes to City Council)
  never-said words   e.g. "unanimous" when nobody said it
  flagged items      how many items the automatic checks flagged
  overview wrong     clauses of the overview that state a vote's outcome differently
                     from the official record (the overview is what most people read)
  speakers named     at moments where we know who's talking (from the minutes), does
                     the transcript name them right? (after speakers.py)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVALS_DIR = HERE / "evals"


def matches(decision: dict, vote: dict) -> bool:
    text = (decision.get("motion") or decision.get("description") or "").lower()
    return any(k in text for k in vote["keywords"])


def without_notes(value):
    """Drop the checker's own notes (they mention words like 'unanimous')."""
    if isinstance(value, dict):
        return {k: without_notes(v) for k, v in value.items()
                if k not in ("verify_note", "overview_note", "_meta")}
    if isinstance(value, list):
        return [without_notes(v) for v in value]
    return value


def speaker_checks(key: dict) -> dict:
    """At moments where we know who is talking, did speakers.py name them right?
    This scores the transcript, not a summary, so it's printed once per meeting."""
    path = HERE / "transcripts" / f"{key['video_id']}.json"
    checks = key.get("speaker_checks", [])
    if not checks or not path.exists():
        return {}
    segments = json.loads(path.read_text())["segments"]
    turns = [t for s in segments for t in (s.get("turns") or ([s] if "speaker" in s else []))]
    if not turns:
        return {}
    counts = {"right": 0, "partial": 0, "wrong": 0, "unnamed": 0}
    details = []
    for check in checks:
        t = check["time"]
        turn = next((x for x in turns if x["start"] <= t <= x["end"]), None) or max(
            (x for x in turns if x["start"] <= t), key=lambda x: x["start"], default={})
        name = turn.get("speaker_name", "")
        expected = check["names"]
        surnames = {n.split()[-1].lower() for n in expected}
        firsts = {n.split()[0].lower() for n in expected if len(n.split()) > 1}
        if not name:
            verdict = "unnamed"
        elif name.split()[-1].lower() in surnames:
            verdict = "right"
        elif len(name.split()) == 1 and any(f.startswith(name.lower()) or name.lower().startswith(f)
                                            for f in firsts):
            verdict = "partial"   # first name only, e.g. "Rich" for Richard Chism
        else:
            verdict = "wrong"
        counts[verdict] += 1
        details.append(f"{verdict.upper():<8} at {t // 60}:{t % 60:02d}  expected {expected[0]}, "
                       f"got {name or turn.get('speaker', '?')}")
    return {**{f"speakers_{k}": v for k, v in counts.items()},
            "speakers_total": len(checks), "speaker_details": details}


# Outcome words a reader would take as the result, mapped to the key's outcomes.
STATED = [
    (re.compile(r"\b(passed|approved|carried|adopted)\b", re.I), "passed"),
    (re.compile(r"\b(failed|denied|rejected)\b", re.I), "failed"),
    (re.compile(r"\b(died|lack of a second)\b", re.I), "died for lack of a second"),
    (re.compile(r"\bwithdrawn\b", re.I), "withdrawn"),
    (re.compile(r"\bunclear\b", re.I), "unclear"),
]
TOO_GENERIC = {"time", "discussion"}   # key words too common to tie a clause to one vote


def overview_checks(summary: dict, key: dict) -> list:
    """Does the overview (the first thing a reader sees) state any vote's
    outcome wrongly? Splits it into clauses; a clause that names a vote and
    states an outcome is checked against the official record."""
    text = " ".join(filter(None, [summary.get("overview", ""), summary.get("overview_votes", "")]))
    clauses = [c for c in re.split(r"[.;:]\s+|,\s*(?:while|and|but)\s+|\s+while\s+", text) if c]
    wrong = []
    for clause in clauses:
        low = clause.lower()
        stated = {outcome for pattern, outcome in STATED if pattern.search(clause)}
        if not stated:
            continue
        for vote in key["votes"]:
            if any(k in low for k in vote["keywords"] if k not in TOO_GENERIC):
                ok = set(vote.get("accept", [vote["outcome"]])) | {vote["outcome"]}
                if stated - ok:
                    wrong.append(f"{vote['id']}: \"{clause.strip()[:90]}\"")
    return wrong


def score(summary: dict, key: dict) -> dict:
    decisions = summary.get("decisions", [])
    used, rows = set(), []
    for vote in key["votes"]:
        candidates = [(abs(d.get("timestamp_seconds", -10**6) - vote["time"]), i)
                      for i, d in enumerate(decisions) if i not in used and matches(d, vote)]
        if not candidates:
            rows.append({"id": vote["id"], "found": False})
            continue
        _, i = min(candidates)
        used.add(i)
        d = decisions[i]
        count = (d.get("vote_count") or "").replace(" ", "").replace("–", "-")
        rows.append({
            "id": vote["id"], "found": True,
            "outcome_ok": d.get("outcome") in vote.get("accept", [vote["outcome"]]),
            # Counts are scored twice: against the official record, and against
            # what was actually said aloud (the two can differ).
            "count_ok": (count in vote.get("accept_counts", [vote["vote_count"]])
                         if vote["vote_count"] else None),
            "spoken_ok": (count == vote["spoken_count"]) if vote.get("spoken_count") else None,
            "link_ok": abs(d.get("timestamp_seconds", -10**6) - vote["time"]) <= 30,
            "got": f"{d.get('outcome')}{' ' + count if count else ''}",
        })

    # A leftover decision that names a real vote but states a different result
    # (e.g. "Rezone to C2: died" when C2 passed) is wrong, not just extra.
    wrong_extras = []
    for i, d in enumerate(decisions):
        if i in used or d.get("outcome") in ("unclear", None):
            continue
        for vote in key["votes"]:
            if matches(d, vote) and d.get("outcome") not in vote.get("accept", [vote["outcome"]]):
                t = int(d.get("timestamp_seconds", 0))
                wrong_extras.append(f"{vote['id']}: '{d.get('outcome')}' for "
                                    f"\"{(d.get('motion') or '')[:60]}\" at {t // 60}:{t % 60:02d}")
                break

    speakers = [c.get("speaker", "") for c in summary.get("public_comments", [])]
    wrong_speakers = sum(1 for s in speakers
                         if not any(p.lower() in s.lower() for p in key["public_speakers"]))
    next_text = " ".join(u.get("item", "") for u in summary.get("upcoming", [])).lower()
    all_text = json.dumps(without_notes(summary)).lower()
    flagged = sum(1 for sec in ("decisions", "discussions", "public_comments", "upcoming",
                                "unclear_audio")
                  for item in summary.get(sec, []) if item.get("verified") is False)
    return {
        "rows": rows,
        "votes_found": sum(r["found"] for r in rows),
        "outcomes_ok": sum(bool(r.get("outcome_ok")) for r in rows),
        # Wrong AND sure of it: the dangerous kind. "unclear" is an honest miss.
        "confidently_wrong": sum(1 for r in rows if r["found"] and not r["outcome_ok"]
                                 and not r["got"].startswith("unclear")) + len(wrong_extras),
        "wrong_extras": wrong_extras,
        "counts_ok": sum(bool(r.get("count_ok")) for r in rows),
        "counts_total": sum(1 for v in key["votes"] if v["vote_count"]),
        "spoken_ok": sum(bool(r.get("spoken_ok")) for r in rows),
        "spoken_total": sum(1 for v in key["votes"] if v.get("spoken_count")),
        "links_ok": sum(bool(r.get("link_ok")) for r in rows),
        "extra_decisions": len(decisions) - len(used),
        "wrong_public_comments": wrong_speakers,
        "public_comments": len(speakers),
        "next_steps_ok": any(k in next_text for k in key["next_steps_keywords"]),
        "never_said_used": [w for w in key["never_said"] if w in all_text],
        "flagged": flagged,
        "overview_flagged": summary.get("overview_verified") is False,
        "overview_wrong": overview_checks(summary, key),
        **speaker_checks(key),
        "model": summary.get("_meta", {}).get("model", "?"),
        "version": summary.get("_meta", {}).get("version", 1),
        "speakers_used": summary.get("_meta", {}).get("speakers"),
        "seconds": summary.get("_meta", {}).get("seconds"),
    }


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print(__doc__)
        return 1
    results = []
    for path in paths:
        video_id = path.name.split(".")[0]
        key_path = EVALS_DIR / f"{video_id}.json"
        if not key_path.exists():
            print(f"[!] No answer key at {key_path}")
            return 1
        key = json.loads(key_path.read_text())
        results.append((path.name, key, score(json.loads(path.read_text()), key)))

    n = len(results[0][1]["votes"])
    col = 22
    print("\n" + "".ljust(26) + "".join(name[:col - 2].ljust(col) for name, _, _ in results))

    def line(label, values):
        print(label.ljust(26) + "".join(str(v).ljust(col) for v in values))

    line("model", [r["model"] for _, _, r in results])
    line("summarizer version", [f"v{r['version']}" for _, _, r in results])
    line("speaker labels used", [{True: "yes", False: "no", None: "-"}[r["speakers_used"]]
                                 for _, _, r in results])
    line("votes found", [f"{r['votes_found']}/{n}" for _, _, r in results])
    line("outcome correct", [f"{r['outcomes_ok']}/{n}" for _, _, r in results])
    line("confidently wrong", [r["confidently_wrong"] for _, _, r in results])
    line("count = official record", [f"{r['counts_ok']}/{r['counts_total']}" for _, _, r in results])
    line("count = what was said", [f"{r['spoken_ok']}/{r['spoken_total']}" for _, _, r in results])
    line("link within 30s", [f"{r['links_ok']}/{n}" for _, _, r in results])
    line("extra decisions", [r["extra_decisions"] for _, _, r in results])
    line("wrong public comments", [f"{r['wrong_public_comments']}/{r['public_comments']}"
                                   for _, _, r in results])
    line("next steps mentioned", ["yes" if r["next_steps_ok"] else "no" for _, _, r in results])
    line("never-said words used", [", ".join(r["never_said_used"]) or "none" for _, _, r in results])
    line("items flagged", [r["flagged"] for _, _, r in results])
    line("overview flagged", ["yes" if r["overview_flagged"] else "no" for _, _, r in results])
    line("overview votes wrong", [len(r["overview_wrong"]) for _, _, r in results])
    line("time (seconds)", [r["seconds"] or "?" for _, _, r in results])

    print("\nPer vote (what each summary said; ~ = matches the audio but not the official record):")
    for i, vote in enumerate(results[0][1]["votes"]):
        expected = vote["outcome"] + (f" {vote['vote_count']}" if vote["vote_count"] else "")
        if vote.get("spoken_count"):
            expected += f" (said aloud: {vote['spoken_count']})"
        if "accept" in vote:
            expected += f"  (also accepted: {', '.join(a for a in vote['accept'] if a != vote['outcome'])})"
        cells = []
        for _, _, r in results:
            row = r["rows"][i]
            if not row["found"]:
                cells.append("MISSING")
            else:
                mark = "✓" if row["outcome_ok"] and row["count_ok"] is not False else (
                    "~" if row["outcome_ok"] and row.get("spoken_ok") else "✗")
                cells.append(f"{mark} {row['got']}")
        print(f"  {vote['label'][:24]:<24}  expected: {expected}")
        print("    " + "".join(c[:col - 2].ljust(col) for c in cells))

    for name, _, r in results:
        for w in r.get("wrong_extras", []):
            print(f"  confidently wrong in {name[:30]}: {w}")
        for w in r["overview_wrong"]:
            print(f"  overview wrong in {name[:30]}: {w}")

    # Speaker names come from the transcript, which every summary of a meeting
    # shares, so they're scored once per meeting rather than once per column.
    shown = set()
    for name, key, r in results:
        if "speakers_total" in r and key["video_id"] not in shown:
            shown.add(key["video_id"])
            print(f"\nSpeaker names in transcripts/{key['video_id']}.json "
                  f"(same for every summary of this meeting):")
            print(f"  right {r['speakers_right']}/{r['speakers_total']}, "
                  f"first name only {r['speakers_partial']}, WRONG {r['speakers_wrong']}, "
                  f"unnamed {r['speakers_unnamed']}")
            for d in r["speaker_details"]:
                print("    " + d)

    EVALS_DIR.mkdir(exist_ok=True)
    with open(EVALS_DIR / "history.jsonl", "a") as f:
        for name, _, r in results:
            r = {k: v for k, v in r.items() if k not in ("rows", "speaker_details")}
            f.write(json.dumps({"file": name, "at": datetime.now(timezone.utc)
                                .isoformat(timespec="seconds"), **r}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
