#!/usr/bin/env python3
"""
watcher.py: step 1 of the meeting pipeline.

Checks the YouTube RSS feeds of the local government channels, keeps only
the videos that are public meetings, and records new ones in meetings.json
with status "new". Later steps (transcribe, summarize) will pick those up.

Standard library only; nothing to install.
Run:  python3 watcher.py
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHANNELS_FILE = HERE / "channels.json"
MEETINGS_FILE = HERE / "meetings.json"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) council-brief/0.1"

DEFAULT_CHANNELS = [
    {"name": "City of Blue Ridge", "handle": "@cityofblueridgegeorgiacity5399", "channel_id": None},
    {"name": "Fannin County EMA", "handle": "@fannincountyema3213", "channel_id": None},
    {"name": "Fannin Rebel TV", "handle": "@FanninRebelTV", "channel_id": None},
]

# Title keyword -> which board the meeting belongs to. Order matters:
# more specific phrases first. Videos matching none are skipped
# (mayor updates, sports streams, etc.).
BOARDS = [
    ("planning commission", "Planning Commission"),
    ("board of commissioners", "Fannin County Board of Commissioners"),
    ("board of education", "Fannin County Board of Education"),
    ("council workshop", "City Council Workshop"),
    ("city council", "City Council"),
]

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


# ---------- small helpers ----------

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------- channel lookup ----------

def resolve_channel_id(handle: str) -> str:
    """Turn an @handle into the UC... channel ID that the RSS feed needs."""
    page = fetch(f"https://www.youtube.com/{handle}")
    for pattern in (r"feeds/videos\.xml\?channel_id=(UC[\w-]{22})",
                    r'"externalId":"(UC[\w-]{22})"',
                    r'"channelId":"(UC[\w-]{22})"'):
        match = re.search(pattern, page)
        if match:
            return match.group(1)
    raise RuntimeError(f"couldn't find a channel ID on the page for {handle}")


# ---------- parsing ----------

def classify(title: str):
    """Return the board name for a meeting title, or None if not a meeting."""
    lower = title.lower()
    for keyword, board in BOARDS:
        if keyword in lower:
            return board
    return None


def meeting_date_from_title(title: str):
    """Pull the meeting date out of titles like 'October 20, 2026 ...'
    or '... 11/12/2026'. Returns 'YYYY-MM-DD' or None."""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", title)
    if m and m.group(1).lower() in MONTHS:
        month, day, year = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", title)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def parse_feed(xml_text: str):
    """Yield one dict per video in a YouTube channel RSS feed."""
    root = ET.fromstring(xml_text)
    for entry in root.findall("atom:entry", NS):
        yield {
            "video_id": entry.findtext("yt:videoId", namespaces=NS),
            "title": (entry.findtext("atom:title", namespaces=NS) or "").strip(),
            "published": entry.findtext("atom:published", namespaces=NS),
        }


# ---------- main ----------

def main() -> int:
    channels = load_json(CHANNELS_FILE, DEFAULT_CHANNELS)
    meetings = load_json(MEETINGS_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_found = []

    for ch in channels:
        try:
            if not ch.get("channel_id"):
                ch["channel_id"] = resolve_channel_id(ch["handle"])
                print(f"Resolved {ch['name']} -> {ch['channel_id']}")
            feed = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['channel_id']}")
        except Exception as err:  # keep going if one channel fails
            print(f"[!] {ch['name']}: {err}", file=sys.stderr)
            continue

        for video in parse_feed(feed):
            board = classify(video["title"])
            if not board or video["video_id"] in meetings:
                continue
            record = {
                **video,
                "board": board,
                "meeting_date": meeting_date_from_title(video["title"]),
                "channel": ch["name"],
                "url": f"https://www.youtube.com/watch?v={video['video_id']}",
                "first_seen": now,
                "status": "new",
            }
            meetings[video["video_id"]] = record
            new_found.append(record)

    save_json(CHANNELS_FILE, channels)  # caches the resolved channel IDs
    save_json(MEETINGS_FILE, meetings)

    if new_found:
        print(f"\n{len(new_found)} new meeting(s):")
        for r in sorted(new_found, key=lambda r: r["meeting_date"] or "", reverse=True):
            print(f"  {r['meeting_date'] or '????-??-??'}  {r['board']:<40} {r['url']}")
    else:
        print("No new meetings.")
    print(f"Tracking {len(meetings)} meeting(s) total in {MEETINGS_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
