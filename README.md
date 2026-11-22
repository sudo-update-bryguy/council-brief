# council-brief

**Free AI summaries, searchable transcripts, and alerts for local government meetings in Blue Ridge and Fannin County, Georgia.**

Local government meetings are public and recorded, but almost nobody watches them. They run 45–70 minutes and most get under 100 views. Paid services already sell AI summaries of these same meetings, with full transcripts and alerts locked behind a subscription. council-brief does the same job for free, in the open.

> 🚧 **Status:** in active development. The pipeline finds new meetings, transcribes them on a local GPU, and summarizes them with a free local model or Gemini's free tier, scored against a hand-checked answer key. The website and alerts are next.

## Meetings covered

| Board | Source |
| --- | --- |
| Blue Ridge City Council (meetings and workshops) | City of Blue Ridge Georgia YouTube channel |
| Blue Ridge Planning Commission | City of Blue Ridge Georgia YouTube channel |
| Fannin County Board of Commissioners | Fannin County EMA YouTube channel |
| Fannin County Board of Education | Fannin Rebel TV YouTube channel |
| Blue Ridge City Council (some meetings) | Mary Walker YouTube channel (linked from the City's Agenda Center) |

## How it works

```
YouTube RSS feeds ──► watcher.py ──► meetings.json (status: new)
                                          │
recording (.mkv) ──► transcribe.py ──► transcripts/<id>.json + .txt (status: transcribed)
                                          │
                        speakers.py ──► who said each line (+ transcripts/<id>.speakers.txt)
                                          │
                       summarize.py ──► summaries/<id>.md + .json (status: summarized)
                                          │   decisions, discussion, public comments, next steps,
                                          │   each linked to the exact second in the video
                        evaluate.py ──► scores a summary against evals/<id>.json (answer key)
```

1. **`watcher.py`** checks each channel's public RSS feed, keeps only meeting videos (sorted by board using title keywords), pulls the meeting date from the title, and queues new meetings.
2. **`transcribe.py`** turns a recording into a timestamped transcript using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3`) on an NVIDIA GPU, with a glossary of local names to improve spelling.
3. **`speakers.py`** works out who is talking. [pyannote](https://github.com/pyannote/pyannote-audio) separates the voices on the GPU, every word is matched to a voice, and then the AI names voices using only evidence in the transcript ("my name is…", being called on by name) plus the board's public roster (`roster.json`). Code checks every name against its evidence and removes the ones it can't confirm. A voice with no evidence stays "Speaker 3".
4. **`summarize.py`** summarizes in passes: topics chunk by chunk, then votes (located by code, interpreted by the model), then an overview written last from the decisions. Code checks every timestamp, quote, vote count, and outcome, and flags anything it can't confirm. Backends are swappable: a local model through [Ollama](https://ollama.com) (default, free), Gemini's free tier, or the Claude API.
5. **`evaluate.py`** scores summaries against a hand-checked answer key: votes found, outcomes and counts correct, links on target, and misattributed speakers. It also checks speaker names at moments where the minutes say who was talking.

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
# Find new meetings (the first run also looks up each channel's ID)
python watcher.py

# Ignore the backlog: mark meetings before a date as skipped
python watcher.py --skip-before 2026-11-01

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
```

```bash
# Summarize with a free local model (needs Ollama: ollama pull qwen3.5:4b)
python summarize.py --video-id su1pgPIVpbM

# Or with Gemini's free tier (GEMINI_API_KEY in .env), saved separately for comparison
python summarize.py --video-id su1pgPIVpbM --backend gemini --tag gemini

# Speaker labels are off for summaries by default (see "Speaker labels" below); --speakers turns them on

# Score both against the answer key
python evaluate.py summaries/su1pgPIVpbM.json summaries/su1pgPIVpbM.gemini.json
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
- [ ] Website: meeting feed, summary pages, full-text search across every transcript
- [ ] Keyword email alerts (no account required)
- [ ] Match summaries to the city's published agenda PDFs
- [ ] "Ask the meeting": chat answers with timestamp sources
- [ ] Grow the answer key to more meetings (built from official minutes)
- [ ] Link official minutes when posted, and flag vote counts that disagree with them
- [x] Speaker identification: voices separated on the GPU, names only with evidence
- [ ] Credit motions and public comments to speakers in code, from the labeled transcript
- [ ] Use the City's Agenda Center as the main meeting list: every meeting's agenda, minutes, and video link, wherever the video is hosted

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

| | v2 | v3 |
| --- | --- | --- |
| Votes found | 6 / 6 | 5 / 6 |
| Confidently wrong | 1 | **0** |
| Vote count matches the official record | 3 / 5 | 2 / 5 |
| Vote count matches what was said aloud | 2 / 2 | 2 / 2 |
| Timestamp link within 30 s | 2 / 6 | **4 / 6** |

v3's checks removed the one confident error (a CBD motion reported as "passed" when it never reached a vote), and links landed twice as often. The first summarizer (v1, audited by hand before the evaluator existed) listed 20 "decisions," most of them not votes.

**Reliability:** Gemini 4 Hydrogen and 3.8 Flash returned "overloaded" (503) on two separate mornings even after 5 retries each, so they couldn't be scored. The local model always ran.

### Speaker identification (Nov 3 meeting)

pyannote found **9 voices in 1 min 42 s** on the RTX 2060. The answer key marks three moments where the minutes say who was talking:

| Moment | Expected | Result |
| --- | --- | --- |
| 0:35, calls the meeting to order | Greg Arnold (chair) | Unnamed: the AI didn't name him in the second run. A code rule now covers this. |
| 3:00, staff report | Jennifer Bates | **Right**, named by code: the chair said "Jennifer, do you want to go over the details" and that voice took the floor |
| 17:20, applicant | Richard Chism | First name only ("Rich") |

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

## License

[AGPL-3.0](LICENSE). Anyone who runs a modified version of this as a service must publish their source code too, so it can't be taken private and put behind a paywall.
