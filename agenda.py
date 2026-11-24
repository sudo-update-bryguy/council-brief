#!/usr/bin/env python3
"""
agenda.py: read a meeting's official agenda (or full meeting packet).

The agenda is posted before the meeting, so it's the first official word on
what the meeting is about and who will be in the room. This script reads it
into agendas/<id>.json:

  1. the agenda items, in order ("3c. Review of Utility Extension and Service
     Policy"). summarize.py files each part of the summary under the item it
     belongs to, so a reader can jump straight to the part they care about;
  2. the people it names, with their titles: department heads presenting an
     item ("Public Works Director Zack Ratcliff regarding..."), applicants
     ("Variance request from Ronald B. George, III"), and people who signed
     documents in the packet ("Richard A Chism, President"). transcribe.py
     gives those names to Whisper so it spells them right, and speakers.py
     adds them to the roster for that one meeting, so a voice can be named
     after them when the evidence says so;
  3. organizations that appear as applicants ("Heron Hotels, LLC"), for
     Whisper's spelling list.

Usage:
  python agenda.py --video-id su1pgPIVpbM              (downloads the packet or agenda)
  python agenda.py --video-id su1pgPIVpbM --pdf packet.pdf
Run it before transcribe.py when you can; it helps every step after it.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from minutes import download, pdf_text

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
AGENDAS_DIR = HERE / "agendas"

# Words that make a phrase a job title.
ROLE = (r"Director|Coordinator|Sheriff|Clerk|CFO|Manager|Chief|Administrator|Attorney|Engineer|"
        r"Superintendent|Officer|Chairman|Chairwoman|President|Planner|Treasurer|Marshal|Inspector")
PERSON = r"[A-Z][a-z'’]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z'’]+)?\s+[A-Z][A-Za-z'’-]+"
PROCEDURAL = re.compile(r"\b(call(ed)? (the )?meeting to order|prayer|pledge|invocation|adjourn\w*|"
                        r"executive session|approv\w* (of )?(the )?(minutes|agenda))\b", re.I)


# ---------- 1. agenda items ----------

def agenda_pages(text: str) -> str:
    """The agenda itself: page 1 of a city packet ("Page 1 of 104"), or the
    first two pages of a stand-alone agenda (county agendas run long)."""
    pages = text.split("\f")
    if re.search(r"Page 1 of \d+", pages[0]):
        return pages[0]
    return "\f".join(pages[:2])


def parse_items(text: str) -> list:
    """Numbered items ("4. Public Comments"), lettered sub-items ("a. Review of
    ..."), and section lines with a body ("CLOSING: Adjournment"). Numbering
    restarts in each section on county agendas, so items keep their section."""
    items, section, last = [], "", None
    for raw in agenda_pages(text).splitlines():
        line = raw.strip()
        if not line or line.startswith(("•", "!", "")) or re.match(r"^(pg\.|Page) ?\d", line):
            last = None if not line else last
            continue
        if re.match(r"^Our Mission Statement", line):
            break
        m = re.match(r"^(\d{1,2})\s*\.\s*(\S.*)$", line)
        sub = re.match(r"^([a-z])\.\s+(\S.*)$", line)
        head = re.match(r"^([A-Z][A-Z &/-]{3,}):\s*(.*)$", line)
        if head:
            section = head.group(1).title()
            body = head.group(2) or (section if re.search(r"public comment|executive session",
                                                          section, re.I) else "")
            if body:
                last = {"number": "", "section": section, "text": body}
                items.append(last)
            else:
                last = None
        elif m:
            last = {"number": m.group(1), "section": section, "text": m.group(2)}
            items.append(last)
        elif sub and items:
            parent = next((i["number"] for i in reversed(items) if i["number"].isdigit()), "")
            last = {"number": parent + sub.group(1), "section": section, "text": sub.group(2)}
            items.append(last)
        elif last is not None and raw.startswith(" ") and len(last["text"]) < 400:
            last["text"] += " " + line   # a long item wraps onto the next line
    for item in items:
        item["text"] = re.sub(r"\s+", " ", item["text"]).strip()
        parent = next((i for i in items if i["number"] and i["number"] == item["number"][:-1]
                       and item["number"][-1:].isalpha()), None)
        # "Consideration of a Motion to enter into Executive Session pursuant to
        # O.C.G.A. ..." is boilerplate: keep the part a reader needs.
        title = re.sub(r"^Board Discussion\s*/\s*Action\s*[:–-]?\s*", "", item["text"])
        item["title"] = re.split(r"\s+(?:pursuant to|O\.C\.G\.A\.)\s", title)[0][:180]
        item["procedural"] = bool(PROCEDURAL.search(item["text"])) or bool(parent and parent["procedural"])
    return items


# ---------- 2. people and organizations ----------

def clean_person(name: str) -> str:
    """'Richard A Chism' -> 'Richard Chism'; 'Sherri L. Walker' -> 'Sherri Walker'."""
    words = [w for w in name.replace("’", "'").split()
             if not re.fullmatch(r"[A-Z]\.?|CPA|PE|AICP|Esq\.?|Jr\.?|Sr\.?|I{2,3}", w)]
    return " ".join(words)


def parse_people(text: str, items: list) -> tuple:
    people, orgs = {}, []

    def add(name, title, source):
        name = clean_person(name)
        if len(name.split()) >= 2 and name not in people:
            people[name] = {"name": name, "title": title.strip(" ,"), "source": source}

    for item in items:
        t = item["text"]
        # "Public Works Director Zack Ratcliff regarding the purchase of equipment."
        for m in re.finditer(rf"((?:[A-Z][A-Za-z&.]*\s){{0,4}}?(?:{ROLE}))\s+({PERSON})\s+"
                             r"(?:regarding|to present|to discuss|presenting|for)\b", t):
            title = re.sub(r"^(Fannin County|City of Blue Ridge|Board Discussion/Action:?)\s*", "",
                           m.group(1)).strip()
            add(m.group(2), title, "presents an agenda item")
        # "Variance request from Ronald B. George, III" / "Appointment of Lisa Dougherty"
        for m in re.finditer(rf"\b(?:request (?:from|for)|(?:Re-?)?appointment of|Resignation of)\s+({PERSON})", t):
            add(m.group(1), "named in an agenda item", "agenda item")
        for m in re.finditer(r"\b(?:request|application) (?:from|by|for)\s+([A-Z][\w&.,' ]+?(?:LLC|Inc\.?|Corp\.?|Company))", t):
            orgs.append(m.group(1).strip(" ,"))
    # The packet: "Richard A Chism, President", "Rebecca Harkins, Utility Director",
    # "APPLICANT: Heron Hotels, LLC".
    for line in text.splitlines():
        line = line.strip()
        m = re.fullmatch(rf"({PERSON})(?:\s+(?:CPA|PE|AICP|Esq\.?))?,\s+((?:[A-Z][a-z]+\s){{0,3}}(?:{ROLE}))", line)
        if m:
            add(m.group(1), m.group(2), "signed a document in the meeting packet")
        m = re.match(r"^APPLICANT:\s+(.+)$", line)
        if m:
            orgs.append(m.group(1).strip())
    return list(people.values()), list(dict.fromkeys(orgs))


def load(vid: str) -> dict:
    path = AGENDAS_DIR / f"{vid}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def agenda_people(vid: str) -> list:
    """People the agenda names, as roster entries: [{"name", "title"}]."""
    return [{"name": p["name"], "title": p["title"] if p["title"] != "named in an agenda item"
             else "named on the agenda"} for p in load(vid).get("people", [])]


def agenda_terms(vid: str) -> str:
    """Names and organizations from the agenda, for Whisper's spelling list."""
    data = load(vid)
    names = [p["name"] for p in data.get("people", [])] + data.get("organizations", [])
    return (", ".join(names) + ".") if names else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a meeting's official agenda or packet.")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--pdf", type=Path, help="an agenda or packet PDF you saved, instead of downloading it")
    args = parser.parse_args()
    vid = args.video_id

    meetings = json.loads(MEETINGS_FILE.read_text()) if MEETINGS_FILE.exists() else {}
    meeting = meetings.get(vid, {})
    AGENDAS_DIR.mkdir(exist_ok=True)
    pdf = AGENDAS_DIR / f"{vid}.pdf"
    official = meeting.get("official", {})
    if args.pdf:
        shutil.copyfile(args.pdf, pdf)
    elif not pdf.exists():
        # The packet has the agenda on page 1 plus the documents behind it,
        # which is where applicants and staff sign their names.
        url = official.get("packet_url") or official.get("agenda_url")
        if not url:
            print("[!] No agenda link for this meeting. Save the agenda or packet PDF and run")
            print("    again with --pdf <file>.")
            return 1
        try:
            download(url, pdf)
        except Exception as err:
            print(f"[!] Couldn't download the agenda ({err}). Save it in your browser and run")
            print("    again with --pdf <file>.")
            return 1

    text = pdf_text(pdf)
    items = parse_items(text)
    people, orgs = parse_people(text, items)
    print(f"Agenda for: {meeting.get('title', vid)}")
    print(f"  {len(items)} item(s):")
    for item in items:
        label = item["number"] or item["section"]
        print(f"    {label:<6} {item['title'][:90]}{'  (procedural)' if item['procedural'] else ''}")
    print(f"\n  {len(people)} people named:")
    for p in people:
        print(f"    {p['name']:<24} {p['title'][:30]:<30} {p['source']}")
    if orgs:
        print(f"\n  Organizations: {', '.join(orgs)}")
    if not items:
        print("\n  [!] No agenda items found. Is this the agenda, or another document?")

    out = AGENDAS_DIR / f"{vid}.json"
    out.write_text(json.dumps({"video_id": vid, "source": official.get("packet_url") or
                               official.get("agenda_url") or "", "items": items,
                               "people": people, "organizations": orgs}, indent=2) + "\n")
    print(f"\nSaved {out.relative_to(HERE)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
