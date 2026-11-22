#!/usr/bin/env python3
"""
agendas.py: the City of Blue Ridge's official meeting list (Agenda Center).

The city posts every City Council and Planning Commission meeting to its
Agenda Center (a CivicPlus site) with the agenda, the packet, the minutes
once approved, and a link to the video, wherever that video is hosted. That
makes it a better meeting list than YouTube: some council meetings are
uploaded to a staff member's personal channel, and video titles don't follow
any format.

This script:
  1. reads the Agenda Center page (one request),
  2. saves every listed meeting to agendas.json,
  3. links each one to meetings.json by its YouTube video ID: adds meetings
     the watcher missed, sets the official date and board, and attaches the
     agenda/packet/minutes links,
  4. reports what changed: new meetings, newly posted minutes (a cue to
     check a summary against the official record), and meetings with no
     video yet.

watcher.py still runs alongside it: it catches videos before the city links
them, and covers the county boards, which aren't in the city's Agenda Center.

Standard library only. Usage:
  python agendas.py                         # check the live site
  python agendas.py --skip-before 2026-11-01
  python agendas.py --html saved_page.html  # parse a page saved from a browser

Known limit: the page lists the current year. In early January, the previous
December's meetings drop off it (the site loads other years with a POST
request this script doesn't make yet).
"""

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from watcher import MEETINGS_FILE, fetch, load_json, save_json

HERE = Path(__file__).resolve().parent
AGENDAS_FILE = HERE / "agendas.json"
SITE = "https://www.cityofblueridgega.gov"
AGENDA_CENTER = f"{SITE}/AgendaCenter"
DEFAULT_LOOKBACK_DAYS = 21   # new meetings older than this are recorded but skipped

YOUTUBE_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/live/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")
ROW_KEY = re.compile(r"^_(\d{2})(\d{2})(\d{4})-(\d+)$")   # _MMDDYYYY-ID


# ---------- 1. parsing the page ----------

class AgendaCenterParser(HTMLParser):
    """Collects one dict per meeting row (<tr class="catAgendaRow">).

    Page layout (CivicPlus): each board is a <div class="listing" id="catN">
    with an <h2> name, then a table whose rows have four cells: the agenda
    link and date, the minutes link, the video link, and a downloads menu
    (HTML / PDF / Packet / Previous Versions)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.category = [], None, None
        self.cell = None          # class of the current <td> ("" for the first cell)
        self.capture = None       # which text we're collecting: "category", "title", "heading", "link"
        self.text, self.link = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "listing" in classes and (a.get("id") or "").startswith("cat"):
            self.category = {"id": a["id"][3:], "name": ""}
        elif tag == "h2" and self.category is not None and not self.category["name"]:
            self.capture, self.text = "category", []
        elif tag == "tr" and "catAgendaRow" in classes:
            self.row = {"category_id": (self.category or {}).get("id"),
                        "category": (self.category or {}).get("name", ""), "heading": ""}
        elif self.row is None:
            return
        elif tag == "td":
            self.cell = a.get("class", "")
        elif tag == "h3":
            self.capture, self.text = "heading", []
        elif tag == "a":
            href = a.get("href")
            name = a.get("name") or ""
            if not href and ROW_KEY.match(name):
                self.row["key"] = name
                return
            if not href:
                return
            url = urljoin(SITE, href)
            if self.cell == "" and "/ViewFile/Agenda/" in href:
                self.capture, self.text, self.link = "title", [], url
            elif self.cell == "minutes" and "/ViewFile/Minutes/" in href:
                self.row["minutes_url"] = url
            elif self.cell == "media":
                self.row["video_url"] = url
            elif self.cell == "downloads":
                if "packet=true" in href:
                    self.row["packet_url"] = url
                elif "html=true" in href:
                    self.row["agenda_html_url"] = url
                elif "/ViewFile/Agenda/" in href:
                    self.row["agenda_pdf_url"] = url

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        text = " ".join("".join(self.text).split())
        if tag == "h2" and self.capture == "category":
            self.category["name"], self.capture = text, None
        elif tag == "h3" and self.capture == "heading":
            self.row["heading"], self.capture = text, None
        elif tag == "a" and self.capture == "title":
            self.row["title"], self.row["agenda_link"], self.capture = text, self.link, None
        elif tag == "td":
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row.get("key"):
                self.rows.append(self.row)
            self.row = None


def clean_row(raw: dict) -> dict:
    """Turn a parsed row into a tidy record."""
    mm, dd, yyyy, agenda_id = ROW_KEY.match(raw["key"]).groups()
    video = YOUTUBE_ID.search(raw.get("video_url", "")) if raw.get("video_url") else None
    amended = re.search(r"Amended (.+)$", raw.get("heading", ""))
    # The agenda itself: the PDF when there is one, else whatever the title links to.
    agenda_url = raw.get("agenda_pdf_url") or (raw.get("agenda_link") or "").replace("?html=true", "")
    return {
        "key": raw["key"],
        "agenda_id": int(agenda_id),
        "meeting_date": f"{yyyy}-{mm}-{dd}",
        "category": raw.get("category", ""),
        "title": raw.get("title", ""),
        "board": board_for(raw.get("category", ""), raw.get("title", "")),
        "agenda_url": agenda_url or None,
        "agenda_html_url": raw.get("agenda_html_url"),
        "packet_url": raw.get("packet_url"),
        "minutes_url": raw.get("minutes_url"),
        "video_url": raw.get("video_url"),
        "video_id": video.group(1) if video else None,
        "amended": amended.group(1) if amended else None,
        "page_url": f"{AGENDA_CENTER}/#{raw['key']}",
    }


def board_for(category: str, title: str) -> str:
    """Use the same board names as watcher.py and roster.json."""
    t = title.lower()
    if category == "Planning Commission":
        return "Planning Commission"
    if category == "Mayor and Council":
        return "City Council Workshop" if "workshop" in t else "City Council"
    return category or "Unknown"


def parse_page(html: str) -> list:
    parser = AgendaCenterParser()
    parser.feed(html)
    return [clean_row(r) for r in parser.rows]


# ---------- 2. linking to meetings.json ----------

def display_title(row: dict) -> str:
    """Many rows are titled just "Mayor and Council Meeting Materials"; add the date."""
    title = re.sub(r"\s+Materials$", "", row["title"]).strip() or row["board"]
    if not re.search(r"\b(19|20)\d{2}\b", title):
        d = date.fromisoformat(row["meeting_date"])
        title = f"{d:%B} {d.day}, {d.year} {title}"
    return title


def official_links(row: dict) -> dict:
    keys = ("key", "agenda_id", "title", "agenda_url", "agenda_html_url", "packet_url",
            "minutes_url", "amended", "page_url")
    return {k: row[k] for k in keys if row.get(k) is not None}


def link_meetings(rows, meetings, skip_before: str, now: str) -> dict:
    """Merge Agenda Center rows into meetings.json. Returns what changed."""
    changes = {"new": [], "skipped": 0, "minutes": [], "corrected": [], "no_video": []}
    for row in rows:
        vid = row["video_id"]
        if not vid:
            if row["meeting_date"] >= skip_before:
                changes["no_video"].append(row)
            continue
        record = meetings.get(vid)
        if record is None:
            record = {
                "video_id": vid, "title": display_title(row), "board": row["board"],
                "channel": "linked from the Agenda Center",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "meeting_date": row["meeting_date"], "date_source": "agenda center",
                "first_seen": now,
                "status": "new" if row["meeting_date"] >= skip_before else "skipped",
            }
            meetings[vid] = record
            if record["status"] == "new":
                changes["new"].append(record)
            else:
                changes["skipped"] += 1
        else:
            # The Agenda Center is the official record: its date and board win.
            for field, value in (("meeting_date", row["meeting_date"]), ("board", row["board"])):
                if record.get(field) != value:
                    changes["corrected"].append((record, field, record.get(field), value))
                    record[field] = value
            record["date_source"] = "agenda center"

        old = record.get("official", {})
        is_new = record.get("first_seen") == now
        if row.get("minutes_url") and not old.get("minutes_url") and not is_new \
                and record["status"] != "skipped":
            changes["minutes"].append(record)
        record["official"] = official_links(row)
    return changes


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Read the city's Agenda Center.")
    parser.add_argument("--html", type=Path, help="parse a saved copy of the page instead")
    parser.add_argument("--skip-before", metavar="YYYY-MM-DD",
                        help=f"record older new meetings as skipped (default: "
                             f"{DEFAULT_LOOKBACK_DAYS} days ago)")
    args = parser.parse_args()
    skip_before = args.skip_before or (date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()

    try:
        html = args.html.read_text(errors="replace") if args.html else fetch(AGENDA_CENTER)
    except Exception as err:
        print(f"[!] Couldn't load the Agenda Center: {err}")
        print("    If the site is blocking scripts, save the page from your browser")
        print("    (Ctrl+S, 'Web Page, HTML only') and run: python agendas.py --html <file>")
        return 1
    rows = parse_page(html)
    if not rows:
        print("[!] No meetings found on the page. The site's layout may have changed.")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meetings = load_json(MEETINGS_FILE, {})
    changes = link_meetings(rows, meetings, skip_before, now)
    save_json(AGENDAS_FILE, {"source": AGENDA_CENTER, "checked_at": now, "meetings": rows})
    save_json(MEETINGS_FILE, meetings)

    boards = {}
    for r in rows:
        boards[r["board"]] = boards.get(r["board"], 0) + 1
    with_video = sum(1 for r in rows if r["video_id"])
    with_minutes = sum(1 for r in rows if r["minutes_url"])
    print(f"Agenda Center: {len(rows)} meetings ({', '.join(f'{n} {b}' for b, n in boards.items())})")
    print(f"  {with_video} have a video link, {with_minutes} have minutes posted")

    if changes["new"]:
        print(f"\n{len(changes['new'])} new meeting(s) added to the queue:")
        for r in sorted(changes["new"], key=lambda r: r["meeting_date"], reverse=True):
            print(f"  {r['meeting_date']}  {r['board']:<24} {r['url']}")
    if changes["skipped"]:
        print(f"\nRecorded {changes['skipped']} older meeting(s) as skipped (before {skip_before}).")
    for record, field, old, new in changes["corrected"]:
        print(f"  Corrected {field} for {record['video_id']}: {old} -> {new}")
    if changes["minutes"]:
        print("\nMinutes newly posted (check these summaries against the official record):")
        for r in changes["minutes"]:
            print(f"  {r['meeting_date']}  {r['board']:<24} {r['official']['minutes_url']}"
                  f"  [{r['status']}]")
    if changes["no_video"]:
        print("\nNo video linked yet:")
        for r in changes["no_video"]:
            print(f"  {r['meeting_date']}  {r['board']:<24} {r['title']}")
    if not (changes["new"] or changes["minutes"] or changes["corrected"]):
        print("\nNothing new since the last check.")
    print(f"\nSaved {AGENDAS_FILE.name}; meetings.json now tracks {len(meetings)} meeting(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
