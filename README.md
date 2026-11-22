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

## How it works

```
YouTube RSS feeds ──► watcher.py ──► meetings.json (status: new)
                                          │
recording (.mkv) ──► transcribe.py ──► transcripts/<id>.json + .txt (status: transcribed)
                                          │
                       summarize.py ──► summaries/<id>.md + .json (status: summarized)
                                          │   decisions, discussion, public comments, next steps,
                                          │   each linked to the exact second in the video
                        evaluate.py ──► scores a summary against evals/<id>.json (answer key)
```

1. **`watcher.py`** checks each channel's public RSS feed, keeps only meeting videos (sorted by board using title keywords), pulls the meeting date from the title, and queues new meetings.
2. **`transcribe.py`** turns a recording into a timestamped transcript using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3`) on an NVIDIA GPU, with a glossary of local names to improve spelling.
3. **`summarize.py`** summarizes in passes: topics chunk by chunk, then votes (located by code, interpreted by the model), then an overview written last from the decisions. Code checks every timestamp, quote, vote count, and outcome, and flags anything it can't confirm. Backends are swappable: a local model through [Ollama](https://ollama.com) (default, free), Gemini's free tier, or the Claude API.
4. **`evaluate.py`** scores summaries against a hand-checked answer key: votes found, outcomes and counts correct, links on target, and misattributed speakers.

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
# Summarize with a free local model (needs Ollama: ollama pull qwen3.5:4b)
python summarize.py --video-id su1pgPIVpbM

# Or with Gemini's free tier (GEMINI_API_KEY in .env), saved separately for comparison
python summarize.py --video-id su1pgPIVpbM --backend gemini --tag gemini

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
- [ ] Publish accuracy results across models and meetings
- [ ] Speaker identification (diarization) so comments are attributed correctly

## Engineering notes

- **Inconsistent titles.** Each channel formats dates differently ("October 20, 2026", "11/12/2026", and some titles have no date at all). The watcher handles several formats and falls back to the YouTube publish date, marking it so it isn't mistaken for the real meeting date.
- **Build as you go, not backfill.** The watcher tracks a status for every meeting (`new` → `transcribed` → …), and `--skip-before` keeps old meetings on record without queueing them.
- **GPU libraries without system CUDA.** faster-whisper needs cuBLAS and cuDNN on `LD_LIBRARY_PATH` *before* Python starts. `transcribe.py` finds the pip-installed libraries and re-launches itself with the right path, so setup is one `pip install`.
- **Fits a 6 GB GPU.** Whisper `large-v3` runs in `int8_float16` mode to fit an RTX 2060. Voice activity detection skips recesses and dead air, and `condition_on_previous_text=False` prevents repeated-text loops on long meetings.
- **A dependency broke upstream.** The newest PyAV (19) removed the `metadata_errors` argument that faster-whisper 1.2.1 passes to `av.open()`, so every transcription failed with a `TypeError`. I traced the traceback to that one argument, confirmed PyAV 16 still supports it, and pinned `av<17` in `requirements.txt`. The pin can come off once faster-whisper updates its call.
- **Quiet source audio.** The city's recordings measure about −35 LUFS, roughly 20 dB below YouTube's usual loudness, and the left channel is 8 dB quieter than the right (likely one off-center microphone). Whisper's input normalization handled it: the test transcript came out clean with no preprocessing, so the pipeline doesn't add a volume boost that could amplify room noise.

- **"Verified" isn't the same as "correct."** The first summarizer checked that every quote existed in the transcript, and only 2 of 70 items failed. A manual audit still found a dozen real errors: votes marked "unclear," a made-up "unanimous," invented reasons for a vote, and commissioners labeled as residents. A real quote can sit under a false claim. That led to the v2/v3 redesign and to `evaluate.py`, which scores against a hand-checked answer key instead of trusting the model's own citations.
- **Let code do what code is good at.** Small models miss votes that fall at a chunk boundary and miscopy long numbers like `2642`. So v2 finds motion and vote language with a regex over the whole transcript and gives the model each vote with context. v3 has the model point at numbered lines (`L14`) instead of seconds, re-anchors anything that doesn't match by searching for the quote, downgrades "passed/failed" to "unclear" when no voting language was spoken nearby, and adds any "died for lack of a second" the model skipped.

## License

[AGPL-3.0](LICENSE). Anyone who runs a modified version of this as a service must publish their source code too, so it can't be taken private and put behind a paywall.
