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
  confidently wrong  a wrong outcome stated as fact (an honest "unclear" doesn't count)
  count correct      vote counts like 3-1, where one was said
  link within 30s    the timestamp lands on the vote
  extra decisions    items in Decisions that aren't real votes
  public comments    entries that aren't one of the real public speakers
  next steps         mentions what happens next (e.g. goes to City Council)
  never-said words   e.g. "unanimous" when nobody said it
  flagged items      how many items the automatic checks flagged
"""

import json
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
            # If an uncertain vote is honestly marked "unclear", a missing count is fine.
            "count_ok": (count == vote["vote_count"] or (not count and "accept" in vote
                         and d.get("outcome") == "unclear")) if vote["vote_count"] else None,
            "link_ok": abs(d.get("timestamp_seconds", -10**6) - vote["time"]) <= 30,
            "got": f"{d.get('outcome')}{' ' + count if count else ''}",
        })

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
                                 and not r["got"].startswith("unclear")),
        "counts_ok": sum(bool(r.get("count_ok")) for r in rows),
        "counts_total": sum(1 for v in key["votes"] if v["vote_count"]),
        "links_ok": sum(bool(r.get("link_ok")) for r in rows),
        "extra_decisions": len(decisions) - len(used),
        "wrong_public_comments": wrong_speakers,
        "public_comments": len(speakers),
        "next_steps_ok": any(k in next_text for k in key["next_steps_keywords"]),
        "never_said_used": [w for w in key["never_said"] if w in all_text],
        "flagged": flagged,
        "overview_flagged": summary.get("overview_verified") is False,
        "model": summary.get("_meta", {}).get("model", "?"),
        "version": summary.get("_meta", {}).get("version", 1),
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
    line("votes found", [f"{r['votes_found']}/{n}" for _, _, r in results])
    line("outcome correct", [f"{r['outcomes_ok']}/{n}" for _, _, r in results])
    line("confidently wrong", [r["confidently_wrong"] for _, _, r in results])
    line("vote count correct", [f"{r['counts_ok']}/{r['counts_total']}" for _, _, r in results])
    line("link within 30s", [f"{r['links_ok']}/{n}" for _, _, r in results])
    line("extra decisions", [r["extra_decisions"] for _, _, r in results])
    line("wrong public comments", [f"{r['wrong_public_comments']}/{r['public_comments']}"
                                   for _, _, r in results])
    line("next steps mentioned", ["yes" if r["next_steps_ok"] else "no" for _, _, r in results])
    line("never-said words used", [", ".join(r["never_said_used"]) or "none" for _, _, r in results])
    line("items flagged", [r["flagged"] for _, _, r in results])
    line("overview flagged", ["yes" if r["overview_flagged"] else "no" for _, _, r in results])
    line("time (seconds)", [r["seconds"] or "?" for _, _, r in results])

    print("\nPer vote (what each summary said):")
    for i, vote in enumerate(results[0][1]["votes"]):
        expected = vote["outcome"] + (f" {vote['vote_count']}" if vote["vote_count"] else "")
        if "accept" in vote:
            expected += f"  (also accepted: {', '.join(a for a in vote['accept'] if a != vote['outcome'])})"
        cells = []
        for _, _, r in results:
            row = r["rows"][i]
            if not row["found"]:
                cells.append("MISSING")
            else:
                mark = "✓" if row["outcome_ok"] and row["count_ok"] is not False else "✗"
                cells.append(f"{mark} {row['got']}")
        print(f"  {vote['label'][:24]:<24}  expected: {expected}")
        print("    " + "".join(c[:col - 2].ljust(col) for c in cells))

    EVALS_DIR.mkdir(exist_ok=True)
    with open(EVALS_DIR / "history.jsonl", "a") as f:
        for name, _, r in results:
            r = {k: v for k, v in r.items() if k != "rows"}
            f.write(json.dumps({"file": name, "at": datetime.now(timezone.utc)
                                .isoformat(timespec="seconds"), **r}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
