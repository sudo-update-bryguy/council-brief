# council-brief

**Free AI summaries and searchable transcripts for local government meetings in Blue Ridge and Fannin County, Georgia.**

Local government meetings are public and recorded, but almost nobody watches them. They run 45–70 minutes and most get under 100 views. Paid services already sell AI summaries of these same meetings, with full transcripts and alerts locked behind a subscription. council-brief does the same job for free, in the open.

> 🚧 **Status:** in active development. The pipeline finds new meetings, transcribes them on a local GPU, summarizes them with a free local model, scores them against answer keys built from the official minutes, and publishes them to a free static website: **[sudo-update-bryguy.github.io/council-brief](https://sudo-update-bryguy.github.io/council-brief/)**.

## Meetings covered

| Board | Meeting list | Video |
| --- | --- | --- |
| Blue Ridge City Council (meetings, workshops, hearings) | City's [Agenda Center](https://www.cityofblueridgega.gov/AgendaCenter) (agenda, packet, minutes) | Whatever YouTube channel the Agenda Center links to |
| Blue Ridge Planning Commission | City's Agenda Center | Same |
| Fannin County Board of Commissioners | YouTube feed | Fannin County EMA channel |
| Fannin County Board of Education | YouTube feed | Fannin Rebel TV channel |

The YouTube feeds also run for the city channels, so a video shows up as soon as it's posted, even before the city links it.

## How it works

```
City Agenda Center ──► agendas.py ──┐  official date, board, agenda/minutes links
YouTube RSS feeds  ──► watcher.py ──┴► meetings.json (status: new)
                                          │
   agenda or packet ──► agenda.py ──► agenda items; names of presenters and applicants
                                          │
recording (.mkv) ──► transcribe.py ──► transcripts/<id>.json + .txt (status: transcribed)
                                          │
                        speakers.py ──► who said each line (+ transcripts/<id>.speakers.txt)
                                          │
        official minutes ──► minutes.py ──► movers, seconders, roll calls; names voices from the record
                                          │
                       summarize.py ──► summaries/<id>.md + .json (status: summarized)
                                          │   decisions, discussion, public comments, next steps,
                                          │   each linked to the exact second in the video
                        evaluate.py ──► scores a summary against evals/<id>.json (answer key)
                                          │
                            site.py ──► docs/ (the website, served by GitHub Pages)
```

1. **`agendas.py`** reads the city's Agenda Center, the official list of every City Council and Planning Commission meeting. Each meeting there links its agenda, packet, approved minutes, and video, wherever the video is hosted. The script adds meetings the YouTube feeds missed, corrects dates and boards to the official record, attaches the links, and reports when minutes are newly posted for a meeting that's already summarized. Every summary then links the official agenda and minutes.
2. **`watcher.py`** checks each channel's public RSS feed, keeps only meeting videos (sorted by board using title keywords), pulls the meeting date from the title, and queues new meetings.
3. **`agenda.py`** reads the meeting's agenda, or the full meeting packet, which is the agenda plus every document behind it. It lists the agenda items in order, and it collects the people the agenda names: staff presenting an item ("Public Works Director Zack Ratcliff regarding..."), applicants, and people who signed documents in the packet ("Richard A Chism, President"). Those names go to Whisper's spelling list and to speaker naming for that one meeting. `summarize.py` then finds where the chair announces each item on the recording and files every vote and discussion topic under the item being discussed at that moment, so each meeting page reads in agenda order.
4. **`transcribe.py`** turns a recording into a timestamped transcript using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3`) on an NVIDIA GPU, with a glossary of local names to improve spelling.
5. **`speakers.py`** works out who is talking. [pyannote](https://github.com/pyannote/pyannote-audio) separates the voices on the GPU, every word is matched to a voice, and then the AI names voices using only evidence in the transcript ("my name is…", being called on by name) plus the board's public roster (`roster.json`). Code checks every name against its evidence and removes the ones it can't confirm. A voice with no evidence stays "Speaker 3".
6. **`summarize.py`** summarizes in passes: topics chunk by chunk, then votes (located by code, interpreted by the model), then an overview of what was discussed, with its votes line written by code from the decisions. Code checks every timestamp, quote, vote count, and outcome, and flags anything it can't confirm. Backends are swappable: a local model through [Ollama](https://ollama.com) (default, free), Gemini's free tier, or the Claude API.
7. **`minutes.py`** runs once a meeting's official minutes are posted (`agendas.py` reports when). It reads the PDF into structured data: every motion with who made and seconded it, the result, and roll-call votes. It then lines the minutes' motions up with the moments in the transcript where a voice says "I make a motion…", and names that voice after the person the minutes credit. A voice is named only when all of its evidence agrees: on Nov 3, the voice the minutes say led the prayer is the same voice that made the CBD motion the minutes credit to William Whaley. Each vote on the site then shows "Moved by…, seconded by…" and the official result, including who voted no. It also spells names the way the minutes do: Whisper writes names by ear, so "Alexis Badnick" becomes "Alexis Budnek" when the first name matches and the last names sound alike.
8. **`evaluate.py`** scores summaries against a hand-checked answer key: votes found, outcomes and counts correct, links on target, and misattributed speakers. It also checks speaker names at moments where the minutes say who was talking.
9. **`site.py`** builds the website into `docs/`: a meeting list, one page per meeting, a search box across every transcript, an RSS feed, and a Support page whose costs and donation goal are computed from `support.json`. It's plain HTML, CSS and a little JavaScript, so GitHub Pages hosts it for free with no server. On each meeting page, the video sits next to a timeline of the recording with a mark where each vote happened. Every timestamp, including those marks, plays the video from that moment, and the official agenda and minutes are one click away. Every page carries link-preview tags (title, summary and a share card) so a meeting shared on Facebook or in a text shows what it is, and `docs/sitemap.xml` lists every page for search engines. Where "Report it" sends people, and the Google Search Console code, are set in `support.json`.

## Setup (Linux)

Developed on CachyOS (Arch-based) with an NVIDIA RTX 2060. Any Linux distro with an NVIDIA driver should work. Without a GPU, transcription falls back to CPU (slower).

```bash
git clone git@github.com:sudo-update-bryguy/council-brief.git
cd council-brief
uv venv --python 3.12
source .venv/bin/activate        # fish shell: source .venv/bin/activate.fish
uv pip install -r requirements.txt
```

You don't need a system-wide CUDA install: NVIDIA's cuBLAS and cuDNN libraries come from pip, and `transcribe.py` puts them on the library path automatically.

## Usage

```bash
# The city's official meeting list: adds meetings, fixes dates, links agendas and minutes
python agendas.py

# Find new meeting videos on YouTube (the first run also looks up each channel's ID)
python watcher.py

# Ignore the backlog: mark meetings before a date as skipped
python watcher.py --skip-before 2026-11-01

# Read the agenda first (downloads the packet; or pass --pdf packet.pdf)
python agenda.py --video-id <video_id>

# Transcribe a recording and link it to a tracked meeting
python transcribe.py path/to/recording.mkv --video-id <video_id> --offset 4

# Or name recordings recordings/<video_id>.mkv and transcribe everything queued
python transcribe.py
```

`--offset` trims lead-in from screen recordings so transcript timestamps line up with the YouTube video.

```bash
# Who's talking (needs HF_TOKEN in .env, after accepting the model's terms at
# huggingface.co/pyannote/speaker-diarization-community-1)
python speakers.py --video-id su1pgPIVpbM

# Once the official minutes are posted: movers, seconders, roll calls, and voice names
python minutes.py --video-id su1pgPIVpbM       # or --pdf saved_minutes.pdf
python summarize.py --video-id su1pgPIVpbM     # cached, so seconds: adds the credits
```

```bash
# Summarize with a free local model (needs Ollama: ollama pull qwen3.5:4b)
python summarize.py --video-id su1pgPIVpbM

# Or with Gemini's free tier (GEMINI_API_KEY in .env), saved separately for comparison
python summarize.py --video-id su1pgPIVpbM --backend gemini --tag gemini

# Speaker labels are off for summaries by default (see "Speaker labels" below); --speakers turns them on

# Score both against the answer key
python evaluate.py summaries/su1pgPIVpbM.json summaries/su1pgPIVpbM.gemini.json

# Build the website from every published (untagged) summary, then preview it
python site.py
python -m http.server -d docs     # open http://localhost:8000

# Donation goal: edit raised_this_year (and updated) in support.json, then rebuild
python site.py

# Publish: commit docs/ and push. GitHub Pages serves it
# (Settings → Pages → Deploy from a branch → main, /docs).
```

## Sample output

Transcribing the opening of the [November 3, 2026 Planning Commission meeting](https://www.youtube.com/watch?v=su1pgPIVpbM) on an RTX 2060:

```
Running on CUDA.

Transcribing: November 3, 2026 City of Blue Ridge Planning Commission Meeting
  Done: 7 segments, 00:01:15 of meeting in 00:00:03 (19.9x real time)
  Saved transcripts/su1pgPIVpbM.txt and su1pgPIVpbM.json
```

`transcripts/su1pgPIVpbM.txt`:

```
[00:00:33] Okay, 6 o'clock. Let's bring the November 3rd Planning Commission to order.
[00:00:38] Doc, would you grace us with a prayer, please?
[00:00:41] Father, we just thank you for this wonderful country where we're allowed to exercise our franchise,
```

At ~20x real time, a full 45-minute meeting transcribes in a little over 2 minutes.

## Roadmap

- [x] Detect new meetings from all three channels
- [x] GPU transcription with timestamps
- [x] AI summaries: decisions, votes, and public comments, each with a clickable timestamp
- [x] Automatic checks on every quote, timestamp, vote count, and outcome
- [x] Answer-key evaluation to compare summarizer versions and models
- [x] Website: meeting list, summary pages with an embedded video and vote timeline, search across every transcript, RSS feed
- [ ] ~~Keyword email alerts (no account required)~~ Dropped from the free version: doing it right costs money (see "Email alerts cost money" below). Listed on the [support page](https://sudo-update-bryguy.github.io/council-brief/support.html) as a funded upgrade.
- [ ] Switch summaries to Claude once donations cover it (the backend is built; see "Support and costs")
- [x] File each summary under the meeting's agenda items, found where the chair announces them on the recording
- [ ] "Ask the meeting": chat answers with timestamp sources
- [ ] Grow the answer key to more meetings (built from official minutes)
- [x] Link the official agenda, packet, and minutes in every summary, and report when minutes are posted
- [x] Show the official result next to the recording's: "3-1, as heard on the recording" / "Official minutes: passed 4-1 (Bill Barber voted no)"
- [x] Speaker identification: voices separated on the GPU, names only with evidence
- [x] Credit public comments to speakers in code, from the voice-labeled transcript
- [x] Credit motions to who made and seconded them, from the official minutes
- [x] Name commissioners' voices from the minutes (who made which motion, who led the prayer)
- [x] Use the City's Agenda Center as the main meeting list: every meeting's agenda, minutes, and video link, wherever the video is hosted
- [x] Pull presenters, applicants and organizations from the agenda and packet into each meeting's roster and Whisper's spelling list

## Support and costs

council-brief costs **$0** to run today: transcription, speaker matching and summaries run on a local GPU with free models, and GitHub Pages hosts the site. The [support page](https://sudo-update-bryguy.github.io/council-brief/support.html) lays out the two upgrades donations would pay for, how each would work, and what each costs:

| Upgrade | How it would work | Per year |
| --- | --- | --- |
| Summaries with Claude Sonnet 5.5 | `summarize.py --backend claude` (already built). Same checks, same scoring against the minutes; only text is sent to the API. About $0.17 for a 45-minute meeting at $2 / $10 per million input / output tokens, budgeted at $0.30 × ~80 meetings a year | $24 |
| Email alerts by topic | Buttondown Basic holds the subscriber list; the pipeline emails subscribers whose topics came up | $90 |
| **Total** | | **$114** |

Every figure is computed by `site.py` from [`support.json`](support.json), including the estimate's assumptions, so the math is public and one file changes when prices do. The page's goal bar fills as donations come in and reads "fully covered" once a year of costs is raised. Donations go through [Ko-fi](https://ko-fi.com/councilbrief), set to charge no platform fee on one-time tips; only the card processor's fee applies. Prices checked November 2026.

## Accuracy results

Every summary is scored with `evaluate.py` against an answer key built from the city's **official minutes**, not from the recording (see "The recording isn't the record" below).

| Meeting | Type | Length | Votes in the minutes | Answer key |
| --- | --- | --- | --- | --- |
| [Nov 3, 2026 Planning Commission](https://www.youtube.com/watch?v=su1pgPIVpbM) | Hearing with votes | 45 min | 6 | [`evals/su1pgPIVpbM.json`](evals/su1pgPIVpbM.json) |
| [Nov 10, 2026 City Council Workshop](https://www.youtube.com/watch?v=Kqv_qusn6rM) | Discussion, public comment | 32 min | 1 | [`evals/Kqv_qusn6rM.json`](evals/Kqv_qusn6rM.json) |

### Local model vs. cloud model (summarizer v3, both meetings combined)

| | Local: `qwen3.5:4b` on an RTX 2060 | Cloud: Gemini 3.5 Flash-Lite (free tier) |
| --- | --- | --- |
| Votes found | **6 / 7** | 2 / 7 |
| Outcome correct | **6 / 7** | 2 / 7 |
| Confidently wrong (wrong outcome stated as fact) | **0** | **0** |
| Timestamp link within 30 s | **5 / 7** | 2 / 7 |
| Public comments credited to the wrong person | 2 / 4 | **0 / 2** |
| Next steps mentioned | 0 / 2 meetings | **2 / 2 meetings** |
| Items flagged by automatic checks | 7 | 0 |
| Run time per meeting | ~1.5–2 min | ~30–40 s |

**The two models fail in opposite ways, and it repeated across both meetings.** The local model catches the votes but writes long, noisy summaries and mislabels speakers (council members called "a resident"). Flash-Lite writes short, clean summaries with correct next steps, but it **silently misses votes**: it found 2 of 6 at the hearing, and at the workshop it said "no formal votes were found" when a motion was made. For a tool whose main job is "what did they decide," a silent omission is the more dangerous failure, and **no automatic check can catch it**. The checks verify what a summary says, not what it leaves out. Only the answer key reveals omissions, which is why it exists.

### Summarizer versions (Nov 3 meeting, local model)

| | v2 | v3 | v6 (current) |
| --- | --- | --- | --- |
| Votes found | 6 / 6 | 5 / 6 | 5 / 6 |
| Confidently wrong | 1 | **0** | **0** |
| Vote count matches the official record | 3 / 5 | 2 / 5 | 2 / 5 |
| Vote count matches what was said aloud | 2 / 2 | 2 / 2 | 2 / 2 |
| Timestamp link within 30 s | 2 / 6 | 4 / 6 | **5 / 6** |
| Overview states a vote wrong | not scored | not scored | **0** (v4: 2) |

### Public comments: who said it (v5 → v6)

| | Nov 3 Planning Commission | Nov 10 Council Workshop |
| --- | --- | --- |
| Named, but the wrong person | 3 of 4 → **0 of 3** | 1 of 6 → **0 of 2** |
| Left unnamed ("A speaker") | 0 of 4 → 2 of 3 | 0 of 6 → 1 of 2 |

v6 decides who made a public comment in code, after the summary is written. Descriptions like "a resident" no longer count as names. A comment from a commissioner or staff member, identified by the name given or by a voice name that passed the evidence checks, moves to Discussion. An unnamed comment gets the voice's name only when that voice was identified with evidence, and back-to-back comments from the same person are combined. The scoring was also split: a blank name is an honest "don't know," and a name that isn't one of the public speakers is wrong. Older summaries are rescored the same way.

v3's checks removed the one confident error (a CBD motion reported as "passed" when it never reached a vote), and links landed twice as often. v5 reports the CBD motion exactly as the minutes do ("died for lack of a second," not just "unclear"), and its overview can no longer contradict the votes (see the engineering notes). Vote counts that disagree with the minutes (3-1 vs. 4-1, 3-2 vs. 2-3) match what was said aloud; the summary reports the recording, and the minutes are linked next to it. The first summarizer (v1, audited by hand before the evaluator existed) listed 20 "decisions," most of them not votes.

**Reliability:** Gemini 4 Hydrogen and 3.8 Flash returned "overloaded" (503) on two separate mornings even after 5 retries each, so they couldn't be scored. The local model always ran.

### Speaker identification (Nov 3 meeting)

pyannote found **9 voices in 1 min 42 s** on the RTX 2060. The answer key marks moments where the minutes say who was talking:

| Moment | Expected | Result |
| --- | --- | --- |
| 0:35, calls the meeting to order | Greg Arnold (chair) | **Right**, named by code: the AI named him in one run and skipped him in the next, so the voice that calls the meeting to order now gets the roster's presiding officer |
| 3:00, staff report | Jennifer Bates | **Right**, named by code: the chair said "Jennifer, do you want to go over the details" and that voice took the floor |
| 17:20, applicant | Richard Chism | **Right**, full name from the minutes (added later; see below). Voice matching alone got only "Rich": his surname never appears in the transcript |
| 0:45, leads the prayer | William Whaley | **Right**, named from the minutes by `minutes.py` (added later; see below) |

**Result: 4 of 4 right, 0 wrong** (3 of 4 right and 1 first name only before the minutes were used). The chair names the person giving the prayer only as "Doc," so the recording alone could never have named him.

### Speaker identification (Nov 10 Council Workshop)

| Moment | Expected | Result |
| --- | --- | --- |
| 0:05, calls the workshop to order | Andy Rhodes (Mayor) | **Right** |
| 1:00, presents the budget | Eric Soroka (consultant) | **Right**, named by code: "I would like to recognize Eric… to present the proposed budget," then that voice took the floor |
| 16:10, council member | Bubba Gibbs | **Right**, named from the minutes: he moved to extend the public speaker's time |
| 25:30, public comment | Alexis Budnek | **Right**: she introduces herself, and the spelling comes from the minutes. Whisper wrote it as "Badnick," and the site showed that until the fix below |

**Result: 4 of 4 right, 0 misspelled, 0 wrong.** The answer key placed the 16:10 check from an unrelated clue (another member says "just reiterate what Bubba said"), so the minutes-based name was confirmed independently.

### Motion credit (from the official minutes)

`minutes.py` read all 6 motions correctly (movers, seconders, results, both roll calls) and named two commissioners' voices: William Whaley (he led the prayer and made the CBD motion) and Mike Panter (the C2 motion). Of the 5 votes in the summary, **5 were credited with the right mover and 0 with the wrong one**. The sixth, the speaking-time extension, isn't in the summary, and Whisper skipped the seconds where it was moved. The phantom "C2" motion the model pulled out of the CBD discussion got no credit, because each motion in the minutes is matched to the vote nearest to where it was actually made. On Nov 10, the one motion (Bubba Gibbs, extending the public speaker's time) was credited correctly, **1 of 1**.

**No wrong names stuck in either run.** The AI proposed "Chuck Conerly" for a voice twice, once with evidence that didn't mention him and once with no introduction. The checks removed both times.

### Speaker labels: do they help the summarizer? (Nov 3, local model)

| | v3 (old transcript, no labels) | v4, no labels | v4, labels on every line, lines split at each speaker change | v4, script-style labels |
| --- | --- | --- | --- | --- |
| Votes found | 5 / 6 | **5 / 6** | 3 / 6 | 3 / 6 |
| Confidently wrong | 0 | 0 | 0 | 0 |
| Count matches the official record | 2 / 5 | 2 / 5 | 1 / 5 | 1 / 5 |
| Timestamp link within 30 s | 4 / 6 | **4 / 6** | 3 / 6 | 2 / 6 |
| Run time | – | 11 min | 12 min | 13 min |

**No, not for the 4B model.** The "no labels" column uses the same new transcript as the labeled runs, with the labels hidden. That separates the two changes: the new transcript scored the same as the old one, and the labels cost 2 of 6 votes in both formats. So labels are off by default, and speaker names will come from the transcript in code instead of through the model.

**Caveats:** two meetings, one run per configuration, and Flash-Lite is Google's smallest model, so this says nothing about larger Gemini models.

## Engineering notes

- **Inconsistent titles.** Each channel formats dates differently ("October 20, 2026", "11/12/2026", and some titles have no date at all). The watcher handles several formats and falls back to the YouTube publish date, marking it so it isn't mistaken for the real meeting date.
- **Build as you go, not backfill.** The watcher tracks a status for every meeting (`new` → `transcribed` → …), and `--skip-before` keeps old meetings on record without queueing them.
- **GPU libraries without system CUDA.** faster-whisper needs cuBLAS and cuDNN on `LD_LIBRARY_PATH` *before* Python starts. `transcribe.py` finds the pip-installed libraries and re-launches itself with the right path, so setup is one `pip install`.
- **Fits a 6 GB GPU.** Whisper `large-v3` runs in `int8_float16` mode to fit an RTX 2060. Voice activity detection skips recesses and dead air, and `condition_on_previous_text=False` prevents repeated-text loops on long meetings.
- **A dependency broke upstream.** The newest PyAV (19) removed the `metadata_errors` argument that faster-whisper 1.2.1 passes to `av.open()`, so every transcription failed with a `TypeError`. I traced the traceback to that one argument, confirmed PyAV 16 still supports it, and pinned `av<17` in `requirements.txt`. The pin can come off once faster-whisper updates its call.
- **Quiet source audio.** The city's recordings measure about −35 LUFS, roughly 20 dB below YouTube's usual loudness, and the left channel is 8 dB quieter than the right (likely one off-center microphone). Whisper's input normalization handled it: the test transcript came out clean with no preprocessing, so the pipeline doesn't add a volume boost that could amplify room noise.

- **"Verified" isn't the same as "correct."** The first summarizer checked that every quote existed in the transcript, and only 2 of 70 items failed. A manual audit still found a dozen real errors: votes marked "unclear," a made-up "unanimous," invented reasons for a vote, and commissioners labeled as residents. A real quote can sit under a false claim. That led to the v2/v3 redesign and to `evaluate.py`, which scores against a hand-checked answer key instead of trusting the model's own citations.
- **Let code do what code is good at.** Small models miss votes that fall at a chunk boundary and miscopy long numbers like `2642`. So v2 finds motion and vote language with a regex over the whole transcript and gives the model each vote with context. v3 has the model point at numbered lines (`L14`) instead of seconds, re-anchors anything that doesn't match by searching for the quote, downgrades "passed/failed" to "unclear" when no voting language was spoken nearby, and adds any "died for lack of a second" the model skipped.

- **Measure the dangerous mistakes separately.** A plain accuracy score treats "unclear" and a wrong "passed" the same. `evaluate.py` adds a *confidently wrong* count, and the design choice behind v3 (downgrade to "unclear" whenever the evidence isn't there) only shows up in that number.
- **The evaluator caught a bug in my own safety check.** v3's first run scored *worse* on outcomes (3/5 vs 4/5). Printing each decision's check notes showed why: the "is there voting language near this?" check only looked 3 lines past the motion, but "All in favor? … Aye." came 4–5 lines later, so a correct "passed" was downgraded. Switching to a 30-second window fixed it. Without the answer key, that regression would have shipped silently.
- **Ask small models multiple-choice questions.** The 4B model never connected "my motion died for lack of a second" (43:02) to the CBD motion made ten minutes earlier (32:38). Code already spots "died for lack of a second," so it now lists the earlier unresolved motions and asks the model *which one*. The model handles that narrow choice well even though it misses it in open-ended reading.
- **Free tiers fail in ordinary ways.** Gemini's free tier returns rate limits (429), "model overloaded" (503), and sometimes no response at all. The client retries all three with increasing waits, and every successful AI answer is cached on disk, so a run that dies halfway resumes without spending quota twice.
- **The recording isn't the record.** The first answer key came from the transcript, so it inherited the transcript's mistakes. The official minutes showed the C2 rezoning passed **4–1** (the recording says "3-1"), the permit failed **2–3**, the CBD motion was ruled out of order rather than voted on, and there was a sixth vote (extending the applicant's speaking time) that the key had dismissed as noise. The key is now built from the minutes, and `evaluate.py` scores vote counts both against the record and against what was said aloud, since a summary of the recording can't know whether the count was misspoken or misheard. The minutes also supplied the officials' real names, which now go into Whisper's glossary (the applicant is "Richard Chism," not "Rich Chisholm").
- **More information made the small model worse.** Speaker labels seemed like pure upside, since the model could finally tell a commissioner from a resident. They cost 2 of 6 votes. The first version split Whisper's lines every time the voice detector switched speakers, so "can I get a motion for approval" became four lines from three "people." The second version kept Whisper's lines and showed names only when the speaker changed, but it still lost the same two votes. Hiding the labels from the exact same transcript brought them back. That one control run kept the pipeline from blaming the new transcript and kept speaker labels in the model's prompt from becoming the default on a hunch.
- **Score each stage on its own.** At first the evaluator printed speaker accuracy in every summary's column. Speaker names come from the transcript, though, so every column showed the same number and it looked like the summarizer was being graded on it. Names are now scored once per meeting, and a first name alone ("Rich" for Richard Chism) counts separately from a wrong name.
- **Narrow rules beat open-ended guessing for names.** The AI names voices from evidence, and code removes any name the evidence doesn't support. Code also fills gaps with three narrow rules, each tested offline against cases that should *not* fire: the voice that calls the meeting to order is the roster's presiding officer; a voice that takes the floor right after "Jennifer, do you want to…" is Jennifer (but "Thank you, Jennifer" doesn't count); and "Rich" becomes the full name only if that voice said "I'm Rich Chism" (but "I'm rich in experience" doesn't match). Diarization also flickers between voices for single words, so one-word switches are smoothed out, except words that matter in a vote ("second," "aye," "no").

- **Go to the official source.** The YouTube watcher missed the Nov 10 and Nov 17 council meetings for days: they had been uploaded to a staff member's personal channel, and they only turned up after that channel was added by hand. The city's Agenda Center lists every meeting with its video link, wherever the video is hosted, so it's now the main meeting list and the feeds are the early warning. Its dates also win over the watcher's, which sometimes has to fall back to YouTube's upload date. The page is parsed with Python's built-in HTML parser instead of regex patterns, and the script was tested on a saved copy of the page. If the layout changes, it fails loudly ("no meetings found") instead of silently returning nothing. On the first real run, it read 32 meetings, linked 27 to videos, and flagged minutes as newly posted for both summarized test meetings.
- **"Official" doesn't mean "specific."** That first run also "corrected" three council workshops to regular meetings. The Agenda Center titles most rows just "Mayor and Council Meeting Materials," and my code read that as "not a workshop." The YouTube titles had been right. Now the agenda only overrides the board when its title actually names one ("City Council Meeting," "Workshop," "Special Called"). Otherwise the video title decides, and that rule also repaired the three records on the next run.
- **Don't let the model retell what code already knows.** The overview step was given the correct decisions list and still scrambled it: "the CBD motion failed due to unclear audio... a C2 motion died for lack of a second." In the official minutes, the CBD motion died and the C2 rezoning passed. The automatic checks only looked for invented vote counts and "unanimous," so this passed them, and the evaluator didn't score overviews at all. Now code writes the overview's vote line straight from the decisions list. The model only describes what was discussed, and any sentence it writes about votes is removed. `evaluate.py` also scores the overview against the minutes. The old overview has 2 wrong statements. In the new format, the overview can only be as wrong as the decisions list, which is already scored vote by vote.
- **An "extra" can be a wrong answer.** The first live page showed "Rezone the property to C2: died for lack of a second." The minutes say the CBD motion died and the C2 rezoning passed a minute later. My first guess at the cause was wrong. Printing the item's own check notes showed what actually happened: the model had pulled a second, phantom "C2" motion out of the CBD discussion, and when code asked "which earlier motion died?", the model picked the phantom. Two fixes followed. A motion that gets a real vote within three minutes can't be the one that died, so it's no longer offered as a choice, and the same rule strips a "died" label the model writes itself. The evaluator also had this filed under "extra decisions." It now counts any item that names a real vote with the wrong result as *confidently wrong*. On the Nov 3 meeting, confidently wrong went from 1 to 0, the CBD motion is now reported exactly as the minutes record it, and links within 30 seconds rose from 4 of 6 to 5 of 6.
- **The same number, spelled two ways.** The check that removes vote counts nobody said looked only for digits. After the transcript was regenerated with a better glossary, Whisper wrote the permit tally as "three two" where it had written "3-2" before, so the check removed a count that really was said ("count matches what was said aloud" fell from 2 of 2 to 1 of 2). Number words now count as numbers, and the score is back to 2 of 2. Invented counts are still removed. The same pass fixed an overview that began "The council *also* reviewed…": when the first sentence is dropped for mentioning a vote, a leftover "also" in the new opening line is dropped too.
- **A description isn't a name.** The first version of speaker crediting changed nothing: no comment was unnamed, so its rules never fired. A diagnostic run printed each public comment's name next to the voice heard at that moment. The model had written "a resident" for three speakers, and the voice match showed one of them was the City Clerk. Descriptions are now treated as unnamed, the clerk's remarks moved to Discussion, and the other two became an honest "A speaker." Voice matching only decides for comments the model left unnamed, because a timestamp a few seconds off can land on the chair instead of the commenter.
- **Email alerts cost money, so they're out of the free version.** Alerts were in the original pitch, so I worked through the options before cutting them. *A feed reader* (RSS) was free and already built, but few residents use one. *Running email myself* meant holding a list that reveals where people live and what issues they follow (someone subscribed to "Shamrock Road" probably lives near it). It would also need a signup backend for a static site, double opt-in, signed unsubscribe links, bot protection, a domain with SPF/DKIM/DMARC so mail isn't marked as spam, and care that GitHub Actions logs (public on a public repo) never print an address. *A newsletter service* would handle all of that, but checking the actual plans showed that Buttondown's free tier doesn't include API sending or subscriber tags, and MailerLite's free tier doesn't include API sending. Automatic topic alerts start around $9 a month. The project's rule is free first and paid only once donations cover it, so alerts moved to the support page as a costed upgrade instead of being built half-way or built on people's personal data. The RSS feed stays for anyone who uses one.
- **The minutes name the voices.** Voice matching could name the chair, the clerk and the applicant, because the recording says who they are. Commissioners mostly don't introduce themselves, so they stayed "Speaker 2" and "Speaker 6". The official minutes fill that gap: they say who made each motion and who led the prayer. Lining those up with the moments a voice says "I make a motion…" names the voice, but only when every piece of evidence agrees. Speaker 2 was named William Whaley because the prayer and the CBD motion both point to him. "Second" never names a voice: in the same meeting, a one-word "Second." was credited to the voice that had just made the motion, while the minutes say a different commissioner seconded it. Parsing the minutes had its own trap. "Speaking in Opposition- No One" looked like a roll-call vote ("Opposition: No"), so roll-call names must now be actual board members.
- **A sentence split between two voices.** On Nov 10, `minutes.py` couldn't find Bubba Gibbs's motion even though it's clearly on the recording. Printing the turns around it showed why: voice matching gave "…how do we handle that **i make**" to the public speaker and "**a motion to extend** your time" to the council member. No single turn contained "I make a motion," and the search looked inside one turn at a time. Now it searches each turn together with the next, and credits the voice that says the word "motion," because that voice is stating the motion. That named Speaker 3 as Bubba Gibbs, which the answer key's separate check confirmed. Two smaller misses were fixed in the same pass: the mayor's "I would like to **recognize** Eric… to present" didn't count as calling someone forward, so Eric Soroka went unnamed until "recognize" and "present" were added; and Nov 3 still scored the same after the change, which is how I checked the fix didn't break the meeting that already worked.
- **A name heard isn't a name spelled.** The Nov 10 public speaker said her name clearly, and Whisper wrote what it heard: "Alexis Badnick." The minutes spell it "Alexis Budnek," and the live site used the wrong spelling. The evaluator called it right because the answer key listed both spellings, so a misspelled name scored the same as a correct one. Now `minutes.py` collects every name the minutes mention (public commenters, people speaking for or against, movers, seconders) and corrects a voice's name only when the first name matches and the last name *sounds* the same. For that it uses Soundex, a decades-old code for matching names by sound: Badnick and Budnek are both B352. It has to be the only match, too. A first name alone is completed only for a member of the public, and only when exactly one person in the minutes fits, so "Rich" became Richard Chism (listed as speaking in favor), which also took Nov 3 from 3 of 4 right to 4 of 4. `summarize.py` applies the same spellings to the summary text but leaves quotes alone, because quotes are checked word for word against the transcript. A lone first name is fixed only in the speaker field, since in prose "Rich" could be part of "Rich Mountain." The evaluator now has a **misspelled** category: the first spelling in the answer key is the official one, and any other spelling counts as the right person but the wrong spelling. Run against the old transcript, it reports exactly this bug.
- **The county doesn't run its meetings like the city.** The first county meeting (Board of Commissioners, Oct 27) broke two of the city-tuned naming rules. First, "when I was at the Board of **Ed**, is this road up?" named a 25-minute public commenter after a maintenance director called Ed: a roster first name plus a question mark looked like the chair calling on someone. A name after "of" or "the" no longer counts, and when a question mark is the only cue, the name has to be in direct address ("Zach, have you ever worked on this road?"). Second, the chair went unnamed because nobody says "call to order": the commission opens with the prayer and goes straight to the minutes. Now, if no one calls the meeting to order, the chair is the voice that runs it (asks for seconds, calls "all in favor," opens executive session), with at least 4 such lines and twice as many as anyone else. On Oct 27 that was 15 lines to 1. The same run showed the roster was stale: the meeting introduces a new maintenance director, so the old one came off. Rosters drift, and each new board is a fresh test of rules that were written for another one.
- **Words alone can't tell which agenda item a topic belongs to.** The first version filed each summary topic under the agenda item it shared the most words with. On Nov 10 it put most of the budget review under "Utility Extension and Service Policy": budget topics like "Utility Rate Increases" and "Debt Service Fund" share "utility" and "service" with it, and most budget topics never say "budget." A version that also kept neighboring topics together still got it wrong. What works is the recording itself: chairs announce items, often in the agenda's own words ("Our next discussion item is the comprehensive pay plan"). `summarize.py` finds each announcement, and a topic belongs to whatever item was announced last before it. Names are left out of the matching, because Whisper mishears them ("ms director baking up" for the EMS director). A tie between two items only starts a new item when the chair says something like "next," so "Director Shannon Wright… purchase of equipment" doesn't jump ahead to the next director's equipment item. Meetings also double back: on Nov 10 the pay plan was announced, set aside while copies were made, and picked up again after the utility policy ("let's go back to that"). A clear match with words like "back to" or "next" re-opens an item. On both city test meetings, every item was placed within seconds of its announcement; the county meeting (tested with an agenda written in the county's format, since its real agenda isn't posted) came out the same.
- **Whisper reads only the end of its spelling list.** faster-whisper keeps the last 223 tokens of the prompt and drops the rest. The single list of every official on every board had grown long enough that adding a meeting's agenda names would have pushed the start of it out of view. The list is now built per meeting: the board's own people from `roster.json`, a few terms per board, and that meeting's agenda names. Each one is shorter than the old shared list.
- **Three more meetings, six more fixes.** Adding the Nov 17 council meeting, the Nov 10 budget hearing and the first school board meeting (Nov 12) turned up problems the earlier meetings didn't have:
  - *An agenda heading beat its own item.* On the budget hearing, "3. Public Hearing" and "3a. Public Input on Proposed FY2027 Budget" both matched "public hearing," the heading won, and every topic landed under the prayer. Headings with sub-items no longer compete.
  - *Long agendas run onto a second page.* Nov 17's action items continued on page 2 of the packet, so votes on a budget resolution and an ordinance were filed under the one item that made it onto page 1. The agenda now continues onto the next page when that page picks up the numbering (item 8 after item 7, or another lettered sub-item).
  - *Reading an item aloud sounds like a vote.* When the Mayor read "consideration of a motion to approve an application for a special exception variance," the model recorded a vote with an unclear result, and the real vote eight minutes later appeared too. An unclear decision is now dropped when the same motion is decided within 20 minutes after it. The first version judged "the same motion" by how alike the text looked, and it removed a real vote: the school board's "Approve the 2026 bond resolution" (one member abstained) looked 55% like "Approve the Unified Literacy Plan." Now the two motions have to share most of their distinctive words; the variance pair shares nine.
  - *A consent agenda passes as one vote.* That vote was filed under the last consent item read, not the consent agenda itself. Items announced together ("the minutes of October 13 and October 20") now go under their shared heading.
  - *Guessed roles were shown as fact.* Unnamed voices were labeled with the AI's guess at their role, and on the budget hearing a business association officer appeared as "Speaker 3 (board member)." The site now shows unnamed voices by number only; a role appears only next to a name that passed the evidence checks.
  - *Some people are handed the floor by title.* The school board chair opens with "I'll turn the meeting over to our superintendent." Code now accepts a title held by exactly one person on the roster (superintendent, clerk, attorney), and "thank you" no longer cancels a hand-off when the same sentence turns the meeting over. The first version still missed her: the superintendent's first words were one line ("first item on the agenda is the invocation") before the pastor prayed, so she didn't "hold the floor" for 20 seconds in the next minute. Someone handed the whole meeting by title gets five minutes instead.
- **The evaluator had bugs too.** Adding official results exposed two of them. First, the minutes say "All in Favor," which I'd stored as "unanimous," and the never-said check flagged it. The minutes' own words are used now, and official-record fields are skipped by that check. Second, a false "overview states a vote wrong": the scorer split "Approve a conditional use permit: failed (2-3 in the official minutes)" at the colon, leaving "failed… minutes" as its own clause, which read as "the minutes vote failed." That split had also been cutting every motion off from its own result, so the code-written votes line was never really being checked. Clauses no longer split at colons, the votes line says "per the official record," and "result unclear" counts as an honest miss rather than a wrong claim. The original scrambled overview is still caught with both of its errors.
- **A static site is enough.** The website has no server, database, or framework. `site.py` writes plain HTML from the same JSON the pipeline already produces, search loads one JSON file of transcript lines when the search box is first used, and GitHub Pages hosts it for free. Every link still works with JavaScript off: timestamps open YouTube at the right second, and with JavaScript on they play the embedded video instead.

## How this was built

I'm the project's author and its only committer, but I didn't write the code by hand. It was written by Claude, Anthropic's AI model, over a long series of conversations. My part:

- **Direction:** I picked the problem (Blue Ridge's meetings get under 100 views), set the scope and the rules: free first, paid only when donations cover it, no personal data. I also decided what got cut, like email alerts.
- **Running it for real:** I recorded the meetings, ran every stage of the pipeline, collected the official minutes, and set up hosting and donations.
- **Review:** I read the output against the recordings and the minutes, reported what looked wrong, and decided what to fix next and what counted as good enough.

Claude wrote the scripts, the website, and drafts of this README, including the engineering notes, which describe problems we worked through together. Commits from Nov 23, 2026 on list Claude as a co-author.

## License

[AGPL-3.0](LICENSE). Anyone who runs a modified version of this as a service must publish their source code too, so it can't be taken private and put behind a paywall.
