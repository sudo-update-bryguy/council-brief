#!/usr/bin/env python3
"""
site.py: build the public website from the pipeline's output.

Reads meetings.json, summaries/<id>.json (the untagged summary is the
published one) and transcripts/<id>.json, and writes a static site to docs/,
which GitHub Pages serves for free. No server, no database, no build tools.

  docs/index.html        every summarized meeting, newest first, plus search
  docs/m/<id>.html       one page per meeting: video, vote timeline, decisions,
                         discussion, public comments, official record, transcript
  docs/about.html        what this is, how it works, how accurate it is
  docs/support.html      what it costs to run, planned upgrades, the donation goal
                         (all figures computed from support.json)
  docs/search.json       every transcript line, for the search box
  docs/feed.xml          RSS feed of new meetings (free alerts in any feed reader)

Standard library only. Usage:
  python site.py
  python -m http.server -d docs     # preview at http://localhost:8000
"""

import argparse
import json
from urllib.parse import quote
import re
import shutil
import sys
from datetime import date, datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
SUMMARIES_DIR = HERE / "summaries"
TRANSCRIPTS_DIR = HERE / "transcripts"
OUT = HERE / "docs"
SUPPORT_FILE = HERE / "support.json"
SITE_URL = "https://sudo-update-bryguy.github.io/council-brief/"
REPO_URL = "https://github.com/sudo-update-bryguy/council-brief"
DEFAULT_DESCRIPTION = ("Free summaries and searchable transcripts of Blue Ridge and Fannin County, "
                       "Georgia public meetings.")
# Set from support.json in main(): where "Report a mistake" goes, and the
# Google Search Console verification code.
SITE = {"report_url": "", "google_site_verification": ""}

OUTCOMES = {   # outcome -> (label, css class)
    "passed": ("Passed", "passed"),
    "failed": ("Failed", "failed"),
    "died for lack of a second": ("Died for lack of a second", "dropped"),
    "withdrawn": ("Withdrawn", "dropped"),
    "tabled": ("Tabled", "dropped"),
    "unclear": ("Result unclear", "unclear"),
}


# ---------- small helpers ----------

def e(text) -> str:
    return escape(str(text or ""), quote=True)


def hms(seconds) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


def weekday(iso: str) -> str:
    return f"{date.fromisoformat(iso):%A}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def video_link(vid: str, seconds: int) -> str:
    return f"https://www.youtube.com/watch?v={vid}&t={int(seconds)}s"


def stamp(vid: str, seconds) -> str:
    """A timestamp that plays the embedded video there (or opens YouTube without JS)."""
    s = int(seconds)
    return (f'<a class="ts" href="{e(video_link(vid, s))}" data-t="{s}" '
            f'aria-label="Play from {hms(s)}">{hms(s)}</a>')


def unchecked(item: dict) -> str:
    if item.get("verified", True):
        return ""
    return f'<span class="check" title="{e(item.get("verify_note"))}">Not confirmed by the automatic checks</span>'


# ---------- loading ----------

def load_meetings():
    """Meetings with a published (untagged) summary, newest first."""
    meetings = json.loads(MEETINGS_FILE.read_text()) if MEETINGS_FILE.exists() else {}
    out = []
    for vid, m in meetings.items():
        summary_path = SUMMARIES_DIR / f"{vid}.json"
        if not summary_path.exists() or not m.get("meeting_date"):
            continue
        transcript_path = TRANSCRIPTS_DIR / f"{vid}.json"
        segments = json.loads(transcript_path.read_text())["segments"] if transcript_path.exists() else []
        out.append({"id": vid, "meeting": m, "summary": json.loads(summary_path.read_text()),
                    "segments": segments,
                    "duration": max((s["end"] for s in segments), default=0)})
    return sorted(out, key=lambda x: (x["meeting"]["meeting_date"], x["id"]), reverse=True)


# ---------- page parts ----------

def short(text: str, limit: int = 200) -> str:
    """Cut at a word boundary: link previews and search results show ~200 characters."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def report_link(subject: str) -> str:
    """Where 'Report a mistake' goes: the project's email or form if one is set
    in support.json, otherwise GitHub issues (which needs a GitHub account)."""
    url = SITE.get("report_url") or f"{REPO_URL}/issues"
    if url.startswith("mailto:") and "?" not in url:
        url += "?subject=" + quote(f"Mistake: {subject}")
    return url


def page(title: str, body: str, depth: int = 0, description: str = "", scripts: str = "",
         wide: bool = False, path: str = "") -> str:
    """path: this page's address under SITE_URL ('' for the home page,
    'm/<id>.html' for a meeting). It's used for the link-preview tags that
    Facebook, iMessage and others read when someone shares the page."""
    up = "../" * depth
    description = short(description or DEFAULT_DESCRIPTION)
    url = SITE_URL + path
    verify = (f'<meta name="google-site-verification" content="{e(SITE["google_site_verification"])}">\n'
              if SITE.get("google_site_verification") and not path else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="{'article' if path.startswith('m/') else 'website'}">
<meta property="og:site_name" content="council-brief">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE_URL}assets/card.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="council-brief: what your local government decided, without sitting through the meeting.">
<meta name="twitter:card" content="summary_large_image">
{verify}<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:ital,wght@0,400;0,600;0,800;1,400&display=swap">
<link rel="stylesheet" href="{up}assets/style.css">
<link rel="alternate" type="application/rss+xml" title="council-brief: new meetings" href="{up}feed.xml">
</head>
<body{' class="wide"' if wide else ''}>
<a class="skip" href="#main">Skip to content</a>
<header class="site">
  <a class="mark" href="{up}index.html">council-brief</a>
  <nav aria-label="Site">
    <a href="{up}index.html">Meetings</a>
    <a href="{up}about.html">About</a>
    <a href="{up}support.html">Support</a>
  </nav>
</header>
<main id="main">
{body}
</main>
<footer class="site">
  <p>Not affiliated with the City of Blue Ridge or Fannin County. Summaries are written by AI from public
  recordings and can be wrong; each one links to the recording and, once posted, the official minutes.</p>
  <p><a href="{REPO_URL}">Source code</a> (AGPL-3.0). Found a mistake? <a href="{e(report_link(title))}">Report it</a>.
  New meetings are also listed in an <a href="{up}feed.xml">RSS feed</a>.</p>
</footer>
<script src="{up}assets/app.js"></script>
{scripts}
</body>
</html>
"""


def strip(item, compact: bool = False) -> str:
    """The meeting as a bar the length of the recording, with a mark at each vote."""
    vid, duration = item["id"], item["duration"]
    decisions = item["summary"]["decisions"]
    if not duration:
        return ""
    marks = []
    for d in decisions:
        label, cls = OUTCOMES.get(d["outcome"], (d["outcome"].capitalize(), "unclear"))
        pos = min(100, max(0, d["timestamp_seconds"] / duration * 100))
        title = f"{hms(d['timestamp_seconds'])}: {label}, {d['motion']}"
        if compact:
            marks.append(f'<span class="vmark {cls}" style="left:{pos:.2f}%" title="{e(title)}"></span>')
        else:
            marks.append(f'<a class="vmark {cls}" style="left:{pos:.2f}%" href="{e(video_link(vid, d["timestamp_seconds"]))}" '
                         f'data-t="{int(d["timestamp_seconds"])}" title="{e(title)}" aria-label="{e(title)}"></a>')
    label = (f"Timeline of the {hms(duration)} recording with "
             f"{plural(len(decisions), 'vote')} marked")
    ends = "" if compact else f'<div class="strip-ends"><span>0:00</span><span>{hms(duration)}</span></div>'
    return (f'<div class="strip{" compact" if compact else ""}" role="group" aria-label="{e(label)}">'
            f'<div class="strip-bar">{"".join(marks)}</div>{ends}</div>')


def motion_text(d: dict) -> str:
    if d["motion"].startswith("A motion (the AI"):
        return "A motion the summary couldn't identify"
    return d["motion"][:1].upper() + d["motion"][1:]


def decisions_html(item) -> str:
    vid = item["id"]
    rows = []
    for d in item["summary"]["decisions"]:
        label, cls = OUTCOMES.get(d["outcome"], (d["outcome"].capitalize(), "unclear"))
        count = f'<span class="count">{e(d["vote_count"])}, as heard on the recording</span>' if d["vote_count"] else ""
        if d.get("moved_by"):
            second = f', seconded by {e(d["seconded_by"])}' if d.get("seconded_by") else ""
            count += f'<span class="count">Moved by {e(d["moved_by"])}{second}</span>'
        official = d.get("official")
        if official:
            result = official["result"] + (f' {official["count"]}' if official.get("count") else "")
            noes = [n for n, v in official.get("roll_call", {}).items() if v == "no"]
            yeses = [n for n, v in official.get("roll_call", {}).items() if v == "yes"]
            minority = (f' ({", ".join(noes)} voted no)' if noes and len(noes) <= len(yeses)
                        else f' ({", ".join(yeses)} voted yes)' if yeses else "")
            count += f'<span class="count official">Official minutes: {e(result)}{e(minority)}</span>'
        where = agenda_title(item["summary"], d.get("agenda_item", ""))
        if where:
            count += f'<span class="count">Agenda item {e(where)}</span>'
        quote = f'<blockquote>“{e(d["quote"])}”</blockquote>' if d.get("quote") else ""
        rows.append(f"""<li class="decision {cls}">
  <div class="outcome">{label}</div>
  <div class="motion"><p>{e(motion_text(d))}</p>{count}{quote}{unchecked(d)}</div>
  <div class="when">{stamp(vid, d['timestamp_seconds'])}</div>
</li>""")
    if not rows:
        return '<p class="empty">No formal votes were found in this recording.</p>'
    return f'<ol class="decisions">{"".join(rows)}</ol>'


def agenda_title(summary: dict, label: str) -> str:
    """'3c' -> '3c. Review of Utility Extension and Service Policy'."""
    item = next((a for a in summary.get("agenda", []) if a["label"] == label), None)
    if not item:
        return ""
    numbered = re.fullmatch(r"(\D+ )?\d+[a-z]?", label)
    return f"{label}. {item['title']}" if numbered else item["title"]


def agenda_section(item, render) -> str:
    """Discussion grouped under the agenda's items, in agenda order. Each item
    links to where it starts on the recording. Items with nothing summarized
    say so; procedural ones (prayer, adjournment) and public comment (which
    has its own section) are left out unless discussion was filed under them."""
    s, vid = item["summary"], item["id"]
    groups = []
    for a in s["agenda"]:
        if a.get("parent"):
            continue
        entries = sorted((d for d in s.get("discussions", []) if d.get("agenda_item") == a["label"]),
                         key=lambda d: d["timestamp_seconds"])
        if not entries and (a.get("procedural") or re.search(r"public comment", a["title"], re.I)):
            continue
        start = a.get("start_seconds")
        when = f'<p class="starts">Starts at {stamp(vid, start)}</p>' if start is not None else ""
        body = (f'<ul class="items">{"".join(render(d) for d in entries)}</ul>' if entries else
                f'<p class="empty">{"No discussion summarized for this item." if start is not None else "Not found on the recording."}</p>')
        groups.append(f'<div class="agenda-item"><h3>{e(agenda_title(s, a["label"]))}</h3>{when}{body}</div>')
    other = sorted((d for d in s.get("discussions", []) if not d.get("agenda_item")),
                   key=lambda d: d["timestamp_seconds"])
    if other:
        groups.append(f'<div class="agenda-item"><h3>Not matched to an agenda item</h3>'
                      f'<ul class="items">{"".join(render(d) for d in other)}</ul></div>')
    return f'<section class="by-agenda"><h2>Discussion, by agenda item</h2>{"".join(groups)}</section>'


def list_section(title: str, items, render) -> str:
    if not items:
        return ""
    return f'<section><h2>{e(title)}</h2><ul class="items">{"".join(render(i) for i in items)}</ul></section>'


def transcript_html(item) -> str:
    vid, segments = item["id"], item["segments"]
    if not segments:
        return ""
    lines, current = [], None
    for seg in segments:
        parts = []
        for turn in seg.get("turns") or [seg]:
            # A role guessed for an unnamed voice ("Speaker 3 (board member)")
            # isn't checked against anything, and on the Nov 10 budget hearing
            # it called a business association officer a board member. Unnamed
            # voices are shown as their number only.
            who = turn.get("speaker_display") if turn.get("speaker_name") else turn.get("speaker")
            if who and who != current:
                parts.append(f'<span class="who">{e(who)}</span> ')
                current = who
            parts.append(e(turn["text"]) + " ")
        s = int(seg["start"])
        lines.append(f'<p id="t{s}" data-t="{s}">{stamp(vid, s)} <span class="said">{"".join(parts).strip()}</span></p>')
    named = any(seg.get("turns") for seg in segments)
    note = ("Speaker names come from voice matching and can be wrong; unnamed voices are numbered."
            if named else "Speakers aren't identified in this transcript yet.")
    return f"""<section id="transcript">
<h2>Transcript</h2>
<p class="note">Machine transcription of the recording. {note} Tap any time to play from there.</p>
<label class="find" for="find">Find in this transcript</label>
<input id="find" type="search" autocomplete="off" placeholder="A road, a business, a name">
<p class="find-count" aria-live="polite"></p>
<div class="lines">{"".join(lines)}</div>
</section>"""


def meeting_page(item) -> str:
    m, s, vid = item["meeting"], item["summary"], item["id"]
    official = m.get("official", {})
    record = []
    if official.get("agenda_url"):
        record.append(f'<a href="{e(official["agenda_url"])}">Agenda</a>')
    if official.get("packet_url"):
        record.append(f'<a href="{e(official["packet_url"])}">Meeting packet</a>')
    if official.get("minutes_url"):
        record.append(f'<a href="{e(official["minutes_url"])}">Official minutes</a>')
    record_html = ""
    if record:
        missing = "" if official.get("minutes_url") else " The minutes haven't been posted yet."
        record_html = (f'<aside class="record"><h2>Official record</h2><p>{" ".join(record)}</p>'
                       f'<p>If this summary and the minutes disagree, the minutes are correct.{missing}</p></aside>')

    grouped = bool(s.get("agenda")) and any(d.get("agenda_item") for d in s.get("discussions", []))
    tag = "h4" if grouped else "h3"

    def discussion(d):
        return (f'<li><{tag}>{e(d["topic"])}</{tag}><p>{e(d["summary"])} {stamp(vid, d["timestamp_seconds"])}</p>'
                f'{unchecked(d)}</li>')

    def comment(c):
        who = e(c["speaker"]) if c.get("speaker") else "A speaker"
        position = "" if c.get("position") in ("", "unclear", None) else f', {e(c["position"])}'
        matched = ' <span class="pos">(name matched by voice)</span>' if "voice" in (c.get("info_note") or "") else ""
        return (f'<li><h3>{who}<span class="pos">{position}</span>{matched}</h3>'
                f'<p>{e(c["summary"])} {stamp(vid, c["timestamp_seconds"])}</p>{unchecked(c)}</li>')

    def upcoming(u):
        return f'<li><p>{e(u["item"])} {stamp(vid, u["timestamp_seconds"])}</p></li>'

    def unclear(u):
        return f'<li><p>{e(u["description"])} {stamp(vid, u["timestamp_seconds"])}</p></li>'

    meta = s.get("_meta", {})
    decisions = s["decisions"]
    # Wide screens: title and overview | video, timeline, official record | votes.
    # Phones: title, video, record, overview, votes (the DOM order below).
    body = f"""<article class="meeting" data-video="{e(vid)}">
<div class="top">
<div class="heading">
  <p class="back"><a href="../index.html">All meetings</a></p>
  <h1>{e(m.get('board', 'Meeting'))}<span class="date">{weekday(m['meeting_date'])}, {long_date(m['meeting_date'])}</span></h1>
</div>
<div class="watch">
  <div class="player"><div id="player" data-video="{e(vid)}">
    <a class="poster" href="{e(m.get('url') or video_link(vid, 0))}">Watch the recording on YouTube</a>
  </div></div>
  {strip(item)}
  {record_html}
</div>
<section class="lede-block">
  <h2 class="visually-hidden">Summary</h2>
  <p class="lede">{e(s.get('overview'))}</p>
</section>
<section class="votes">
  <h2>{plural(len(decisions), 'vote')}</h2>
  {decisions_html(item)}
</section>
</div>
<div class="details">
{agenda_section(item, discussion) if grouped else list_section('Discussion', s.get('discussions'), discussion)}
{list_section('Public comments', s.get('public_comments'), comment)}
{list_section('What happens next', s.get('upcoming'), upcoming)}
{list_section('Hard to hear on the recording', s.get('unclear_audio'), unclear)}
<p class="provenance">Summarized by {e(meta.get('model', 'an AI model'))} from the recording.
{plural(meta.get('unverified_items', 0), 'item')} didn't pass the automatic checks and {'is' if meta.get('unverified_items', 0) == 1 else 'are'} marked.
Every time links to the moment in the video, so you can check it yourself.</p>
</div>
{transcript_html(item)}
</article>"""
    title = f"{m.get('board', 'Meeting')}, {long_date(m['meeting_date'])}"
    return page(title, body, depth=1, description=s.get("overview", ""), wide=True, path=f"m/{item['id']}.html",
                scripts='<script src="https://www.youtube.com/iframe_api" async></script>')


def index_page(items) -> str:
    groups, rows = {}, []
    for item in items:
        month = date.fromisoformat(item["meeting"]["meeting_date"]).strftime("%B %Y")
        groups.setdefault(month, []).append(item)
    for month, group in groups.items():
        entries = []
        for item in group:
            m, s = item["meeting"], item["summary"]
            d = date.fromisoformat(m["meeting_date"])
            first = re.split(r"(?<=[.!?])\s", s.get("overview", "").strip(), maxsplit=1)[0]
            votes = len(s["decisions"])
            entries.append(f"""<li class="entry">
  <time datetime="{m['meeting_date']}"><span class="day">{d.day}</span><span class="dow">{d:%a}</span></time>
  <div>
    <h3><a href="m/{e(item['id'])}.html">{e(m.get('board', 'Meeting'))}</a></h3>
    <p>{e(first)}</p>
    {strip(item, compact=True)}
    <p class="facts"><span>{plural(votes, 'vote')}</span><span>{hms(item['duration'])} recording</span></p>
  </div>
</li>""")
        rows.append(f'<section class="month"><h2>{month}</h2><ol class="entries">{"".join(entries)}</ol></section>')
    listing = "".join(rows) or '<p class="empty">No meetings have been summarized yet.</p>'
    body = f"""<div class="intro">
<h1>What your local government decided, without sitting through the meeting.</h1>
<p>Free summaries and searchable transcripts of Blue Ridge City Council, Planning Commission and
Fannin County meetings, made from the public recordings. Every summary links back to the moment in the
video and to the official minutes.</p>
</div>
<section class="search" aria-labelledby="search-label">
  <label id="search-label" for="q">Search every transcript</label>
  <input id="q" type="search" autocomplete="off" placeholder="Try a road, a business, or a name">
  <p class="search-count" aria-live="polite"></p>
  <ol class="results"></ol>
</section>
{listing}"""
    return page("council-brief: Blue Ridge and Fannin County meetings", body)


def about_page(items) -> str:
    body = f"""<article class="prose">
<h1>About council-brief</h1>
<p>Blue Ridge and Fannin County record their public meetings, but they run close to an hour and few people
can watch them. Paid services sell AI summaries of these same meetings. This site does it for free, and the
code is public so anyone can check how it works.</p>
<h2>How a summary is made</h2>
<ol>
<li>The city's Agenda Center and the boards' YouTube channels are checked for new meetings.</li>
<li>The recording is transcribed on a computer with Whisper, an open speech-recognition model.</li>
<li>Voices are separated and named only when the recording gives evidence, like someone being called on by name.</li>
<li>A small AI model that runs on that same computer writes the summary. Code finds the votes, checks every
quote against the transcript, and marks anything it can't confirm.</li>
</ol>
<h2>How accurate it is</h2>
<p>Each version of the summarizer is scored against answer keys built from the official minutes. The
current version found 5 of 6 votes in the test meeting and never stated a wrong result as fact; when it
can't tell how a vote went, it says the result is unclear. Vote counts are reported as heard on the
recording, which doesn't always match the minutes. <a href="{REPO_URL}#accuracy-results">Full results</a>.</p>
<p>The official minutes are the record. Use this site to find what happened and jump to it, then check the
minutes for anything that matters to you.</p>
<h2>Keeping it running</h2>
<p>The site costs nothing to run today. The <a href="support.html">Support page</a> lists two upgrades that
donations would pay for, better summaries and email alerts by topic, and exactly what each would cost.</p>
<p>{plural(len(items), 'meeting')} summarized so far.</p>
</article>"""
    return page("About council-brief", body, path="about.html")


def money(x: float) -> str:
    return f"${x:,.2f}" if x != int(x) else f"${int(x):,}"


def support_costs(cfg: dict) -> dict:
    """Every figure on the Support page, computed from support.json."""
    c = cfg["claude"]
    per_meeting = (c["input_tokens_per_meeting"] * c["input_per_million"]
                   + c["output_tokens_per_meeting"] * c["output_per_million"]) / 1_000_000
    claude_year = c["budget_per_meeting"] * c["meetings_per_year"]
    email_year = cfg["email"]["per_year"]
    total = claude_year + email_year
    raised = cfg.get("raised_this_year", 0)
    return {"per_meeting": per_meeting, "claude_year": claude_year, "email_year": email_year,
            "total": total, "raised": raised, "share": min(1.0, raised / total) if total else 0}


def support_page(cfg: dict) -> str:
    c, m = cfg["claude"], cfg["email"]
    k = support_costs(cfg)
    covered = k["share"] >= 1
    status = ("This year's costs are fully covered. Thank you. Anything more carries over to next year."
              if covered else f"{money(k['raised'])} raised of {money(k['total'])}, "
                              f"the cost of running both upgrades for a year.")
    if cfg.get("donate_url"):
        give = f'<p><a class="give" href="{e(cfg["donate_url"])}">Donate</a></p>'
    else:
        give = '<p class="note">Donations aren\'t open yet. This page will link to them when they are.</p>'
    body = f"""<article class="prose support">
<h1>Support council-brief</h1>
<p>Today this site costs nothing to run. Transcription, speaker matching and summaries run on a computer
with a free AI model, and GitHub hosts the site for free. Two upgrades would make it better, and both cost
money. Donations would pay for them, and nothing else.</p>

<section class="goal" aria-labelledby="goal-h">
  <h2 id="goal-h">Goal for {e(cfg.get('year_label', ''))}</h2>
  <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="{k['total']:.0f}" aria-valuenow="{k['raised']:.0f}"
       aria-label="Donations raised toward a year of costs">
    <span style="width:{k['share'] * 100:.1f}%"></span>
  </div>
  <p class="goal-status{' done' if covered else ''}">{status}</p>
  {give}
  <p class="note">Updated {e(cfg.get('updated', ''))}. council-brief isn't a registered nonprofit, so donations aren't tax-deductible.</p>
</section>

<h2>Upgrade 1: better summaries with Claude</h2>
<p>Summaries are written today by a small AI model ({e('qwen3.5, 4 billion parameters')}) that runs on the same
computer as everything else. Scored against the official minutes, it finds most votes but misses some, and it
needs a lot of checking code around it. The plan is to switch to {e(c['model'])}, a much stronger model made by
Anthropic, through its paid API.</p>
<p><strong>How it would work:</strong> the summarizer already supports Claude; switching is one setting. Everything
else stays the same: code still finds the votes, checks every quote and count against the transcript, and
scores each version against the official minutes before it's used on the site. Transcription and speaker
matching stay on the local computer, so only text is sent to the API.</p>
<p><strong>What it costs:</strong> {money(c['input_per_million'])} per million word pieces ("tokens") read and
{money(c['output_per_million'])} per million written (<a href="{e(c['price_source'])}">Anthropic's price list</a>).
A 45-minute meeting is about {c['input_tokens_per_meeting']:,} tokens in and {c['output_tokens_per_meeting']:,} out,
about {money(round(k['per_meeting'], 2))}. Budgeting {money(c['budget_per_meeting'])} a meeting for longer ones,
and about {c['meetings_per_year']} meetings a year across the four boards, comes to
<strong>{money(k['claude_year'])} a year</strong>.</p>
<p class="note">{e(c['estimate_basis'])}</p>

<h2>Upgrade 2: email alerts by topic</h2>
<p>Pick topics, like zoning, the budget, a board, or a road, and get an email when a meeting covers one.
This was planned as a free feature and was dropped: doing it for free would mean storing people's email
addresses on this project's own systems, and running the signup, confirmation and unsubscribe pieces that
protect them. A newsletter service does that job better, but not for free.</p>
<p><strong>How it would work:</strong> a signup box on this site, run by {e(m['service'].split()[0])}, where you
choose your topics. {e(m['service'].split()[0])} keeps the list, the confirmations and the unsubscribe links;
this project never stores email addresses. After each new summary, the pipeline sends one email to the
people whose topics came up.</p>
<p><strong>What it costs:</strong> {e(m['service'])}, {money(m['per_month'])} a month or
<strong>{money(m['per_year'])} a year</strong> (<a href="{e(m['price_source'])}">price list</a>).</p>
<p class="note">{e(m['why_not_free'])}</p>

<h2>A year, added up</h2>
<table class="costs">
  <thead><tr><th scope="col">Item</th><th scope="col">Per year</th></tr></thead>
  <tbody>
    <tr><td>Summaries with {e(c['model'])}</td><td>{money(k['claude_year'])}</td></tr>
    <tr><td>Email alerts with {e(m['service'])}</td><td>{money(k['email_year'])}</td></tr>
    <tr><td>Website hosting (GitHub Pages)</td><td>$0</td></tr>
    <tr><td>Transcription and speaker matching (runs locally)</td><td>$0</td></tr>
  </tbody>
  <tfoot><tr><th scope="row">Total</th><td>{money(k['total'])}</td></tr></tfoot>
</table>
<p class="note">Prices checked {e(cfg.get('prices_checked', ''))}. Every number on this page is computed from
<a href="{REPO_URL}/blob/main/support.json">one public file</a>, so you can check the math.</p>
</article>"""
    return page("Support council-brief", body, path="support.html")


def search_index(items) -> dict:
    return {
        "meetings": {i["id"]: {"board": i["meeting"].get("board", ""),
                               "date": long_date(i["meeting"]["meeting_date"])} for i in items},
        "lines": [[i["id"], int(s["start"]), s["text"]] for i in items for s in i["segments"]],
    }


def feed(items) -> str:
    entries = []
    for item in items[:30]:
        m, s = item["meeting"], item["summary"]
        link = f"{SITE_URL}m/{item['id']}.html"
        published = datetime.fromisoformat(s.get("_meta", {}).get("summarized_at")
                                           or f"{m['meeting_date']}T12:00:00+00:00")
        text = f"{s.get('overview', '')} Votes: {s.get('overview_votes', '')}"
        entries.append(f"""<item>
<title>{e(m.get('board', 'Meeting'))}, {long_date(m['meeting_date'])}</title>
<link>{e(link)}</link>
<guid isPermaLink="true">{e(link)}</guid>
<pubDate>{format_datetime(published)}</pubDate>
<description>{e(text)}</description>
</item>""")
    now = format_datetime(datetime.now(timezone.utc))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
<title>council-brief: Blue Ridge and Fannin County meetings</title>
<link>{SITE_URL}</link>
<description>New summaries of local government meetings in Blue Ridge and Fannin County, Georgia.</description>
<language>en-us</language>
<lastBuildDate>{now}</lastBuildDate>
{"".join(entries)}
</channel>
</rss>
"""


# ---------- main ----------

def sitemap(items, has_support: bool) -> str:
    """Every page, for search engines. Submit sitemap.xml in Google Search Console."""
    newest = max(i["meeting"]["meeting_date"] for i in items)
    pages = [("", newest), ("about.html", newest)] + ([("support.html", newest)] if has_support else [])
    pages += [(f"m/{i['id']}.html", i["meeting"]["meeting_date"]) for i in items]
    urls = "".join(f"<url><loc>{SITE_URL}{p}</loc><lastmod>{d}</lastmod></url>\n" for p, d in pages)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n')


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the website into docs/.")
    parser.parse_args()
    items = load_meetings()
    if not items:
        print("[!] No summarized meetings found (summaries/<video_id>.json). Run summarize.py first.")
        return 1

    (OUT / "m").mkdir(parents=True, exist_ok=True)
    (OUT / "assets").mkdir(exist_ok=True)
    for name in ("style.css", "app.js", "card.png"):
        shutil.copyfile(HERE / "web" / name, OUT / "assets" / name)
    (OUT / ".nojekyll").write_text("")          # serve files as-is on GitHub Pages
    support = json.loads(SUPPORT_FILE.read_text()) if SUPPORT_FILE.exists() else None
    for key in SITE:
        SITE[key] = (support or {}).get(key, "")
    (OUT / "index.html").write_text(index_page(items))
    (OUT / "about.html").write_text(about_page(items))
    if support:
        (OUT / "support.html").write_text(support_page(support))
    (OUT / "search.json").write_text(json.dumps(search_index(items), separators=(",", ":")))
    (OUT / "feed.xml").write_text(feed(items))
    (OUT / "sitemap.xml").write_text(sitemap(items, support is not None))
    # No robots.txt: on a project site (github.io/council-brief/) crawlers only
    # read one at the domain's root, which this repo doesn't control. Submit
    # the sitemap in Google Search Console instead.
    keep = set()
    for item in items:
        path = OUT / "m" / f"{item['id']}.html"
        path.write_text(meeting_page(item))
        keep.add(path.name)
    for old in (OUT / "m").glob("*.html"):     # meetings that were unpublished
        if old.name not in keep:
            old.unlink()

    print(f"Built {plural(len(items), 'meeting')} into {OUT.name}/:")
    for item in items:
        m = item["meeting"]
        print(f"  {m['meeting_date']}  {m.get('board', ''):<24} docs/m/{item['id']}.html")
    if support:
        k = support_costs(support)
        print(f"  Support page: {money(k['raised'])} raised of {money(k['total'])} a year "
              f"({k['share']:.0%})")
    print("\nPreview:  python -m http.server -d docs   then open http://localhost:8000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
