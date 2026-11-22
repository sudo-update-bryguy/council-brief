# council-brief

**Free AI summaries, searchable transcripts, and alerts for local government meetings in Blue Ridge and Fannin County, Georgia.**

Local government meetings are public and recorded, but almost nobody watches them. They run 45–70 minutes and most get under 100 views. Paid services already sell AI summaries of these same meetings, with full transcripts and alerts locked behind a subscription. council-brief does the same job for free, in the open.

> 🚧 **Status:** in active development. The pipeline currently finds new meetings and transcribes them on a local GPU. Summaries, the website, and alerts are next.

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
                     (next) summarize ──► decisions, votes, and topics, each linked
                                          to the exact second in the video
```

1. **`watcher.py`** checks each channel's public RSS feed, keeps only meeting videos (sorted by board using title keywords), pulls the meeting date from the title, and queues new meetings.
2. **`transcribe.py`** turns a recording into a timestamped transcript using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3`) on an NVIDIA GPU, with a glossary of local names to improve spelling.

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

## Roadmap

- [x] Detect new meetings from all three channels
- [x] GPU transcription with timestamps
- [ ] AI summaries: decisions, votes, and public comments, each with a clickable timestamp
- [ ] Website: meeting feed, summary pages, full-text search across every transcript
- [ ] Keyword email alerts (no account required)
- [ ] Match summaries to the city's published agenda PDFs
- [ ] "Ask the meeting": chat answers with timestamp sources
- [ ] Accuracy audit: spot-check summaries against the video and publish the error rate

## Engineering notes

- **Inconsistent titles.** Each channel formats dates differently ("October 20, 2026", "11/12/2026", and some titles have no date at all). The watcher handles several formats and falls back to the YouTube publish date, marking it so it isn't mistaken for the real meeting date.
- **Build as you go, not backfill.** The watcher tracks a status for every meeting (`new` → `transcribed` → …), and `--skip-before` keeps old meetings on record without queueing them.
- **GPU libraries without system CUDA.** faster-whisper needs cuBLAS and cuDNN on `LD_LIBRARY_PATH` *before* Python starts. `transcribe.py` finds the pip-installed libraries and re-launches itself with the right path, so setup is one `pip install`.
- **Fits a 6 GB GPU.** Whisper `large-v3` runs in `int8_float16` mode to fit an RTX 2060. Voice activity detection skips recesses and dead air, and `condition_on_previous_text=False` prevents repeated-text loops on long meetings.

## License

[AGPL-3.0](LICENSE). Anyone who runs a modified version of this as a service must publish their source code too, so it can't be taken private and put behind a paywall.
