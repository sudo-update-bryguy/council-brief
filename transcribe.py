#!/usr/bin/env python3
"""
transcribe.py: step 2 of the meeting pipeline.

Turns a meeting recording into a timestamped transcript with faster-whisper,
running on your NVIDIA GPU.

Usage:
  python transcribe.py
      Transcribe every file in recordings/ whose name is a video ID that
      watcher.py marked "new" (e.g. recordings/AbC123xYz.mkv).

  python transcribe.py path/to/recording.mkv --video-id AbC123xYz
      Transcribe one file and link it to a meeting from meetings.json.

  python transcribe.py path/to/recording.mkv
      Transcribe a one-off file that isn't tracked by watcher.py.

Options:
  --offset SECONDS  Seconds of extra footage before the meeting starts in your
                    recording (OBS lead-in), so timestamps line up with YouTube.
  --model NAME      Whisper model (default: large-v3). Try distil-large-v3 for speed.
  --cpu             Force CPU mode (works anywhere, but much slower).

Output:
  transcripts/<id>.json  segments with start/end seconds and word timings (used by later steps)
  transcripts/<id>.txt   human-readable "[hh:mm:ss] text" lines
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_FILE = HERE / "meetings.json"
RECORDINGS_DIR = HERE / "recordings"
TRANSCRIPTS_DIR = HERE / "transcripts"

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".flac", ".ogg"}

# Local names and terms help Whisper spell things correctly. Add council
# members, road names, and businesses as you notice it getting them wrong.
GLOSSARY = (
    "Blue Ridge, Fannin County, Georgia. City Council, Planning Commission, "
    "Board of Commissioners, Board of Education. Mayor Andy Rhodes. "
    # Officials and staff, from official minutes.
    "City Council: Jack Taylor, Bubba Gibbs, Christy Kay, Jacqueline Kiker. "
    "Planning Commission: Greg Arnold, Mike Panter, William Whaley, Will Kirchoffer, Bill Barber. "
    "City Attorney Chuck Conerly, City Clerk Jennifer Bates, Kelsey Ledford, Mary Ann Walker, "
    "consultant Eric Soroka. "
    "County Board of Commissioners: Chairman Jamie Hensley, Johnny Scearce, Glenn Patterson, "
    "former chairman Stan Helton, "
    "County Clerk Sherri Walker, EMA Coordinator Trent Lowery, Public Works Director Zack Ratcliff, "
    "Recreation Director Eddie O'Neal, Sheriff Dane Kirby. "
    "Zoning: M-1, C-2, CBD, conditional use permit."
)


# ---------- GPU library setup ----------

def ensure_cuda_libs_on_path() -> None:
    """faster-whisper needs NVIDIA's cuBLAS and cuDNN libraries, and Linux only
    finds them if LD_LIBRARY_PATH is set *before* Python starts. If they were
    installed with pip, add them to the path and restart this script once."""
    if os.environ.get("COUNCIL_BRIEF_LIBS_SET"):
        return
    try:
        import nvidia.cublas.lib
        import nvidia.cudnn.lib
    except ImportError:
        return  # not pip-installed; rely on system CUDA or CPU fallback

    def lib_dir(module) -> str:
        if getattr(module, "__file__", None):
            return os.path.dirname(module.__file__)
        return list(module.__path__)[0]

    dirs = [lib_dir(nvidia.cublas.lib), lib_dir(nvidia.cudnn.lib)]
    env = dict(os.environ)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(dirs + ([existing] if existing else []))
    env["COUNCIL_BRIEF_LIBS_SET"] = "1"
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


# ---------- helpers ----------

def load_meetings() -> dict:
    if MEETINGS_FILE.exists():
        return json.loads(MEETINGS_FILE.read_text())
    return {}


def save_meetings(meetings: dict) -> None:
    MEETINGS_FILE.write_text(json.dumps(meetings, indent=2) + "\n")


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def find_pending_recordings(meetings: dict):
    """Files in recordings/ named after a video ID that's still 'new'."""
    if not RECORDINGS_DIR.exists():
        return []
    jobs = []
    for f in sorted(RECORDINGS_DIR.iterdir()):
        if f.suffix.lower() in MEDIA_EXTENSIONS and meetings.get(f.stem, {}).get("status") == "new":
            jobs.append((f, f.stem))
    return jobs


# ---------- transcription ----------

def load_model(name: str, force_cpu: bool):
    from faster_whisper import WhisperModel

    if not force_cpu:
        try:
            # int8_float16 fits large-v3 comfortably in the RTX 2060's 6 GB.
            return WhisperModel(name, device="cuda", compute_type="int8_float16"), "cuda"
        except Exception as err:
            print(f"[!] GPU unavailable ({err}). Falling back to CPU, which is much slower.")
    return WhisperModel(name, device="cpu", compute_type="int8"), "cpu"


def transcribe(model, media: Path, offset: float, prompt: str):
    segments, info = model.transcribe(
        str(media),
        language="en",
        beam_size=5,
        vad_filter=True,                   # skip silence (recesses, dead air)
        condition_on_previous_text=False,  # avoids repeated-text loops on long meetings
        initial_prompt=prompt,
        word_timestamps=True,              # lets speakers.py split lines between speakers
    )
    duration = info.duration or 0
    results = []
    for seg in segments:
        if seg.end - offset <= 0:
            continue  # still in the OBS lead-in before the meeting
        start, end = max(0.0, seg.start - offset), seg.end - offset
        text = seg.text.strip()
        if text:
            words = [{"start": round(max(0.0, w.start - offset), 2),
                      "end": round(max(0.0, w.end - offset), 2), "word": w.word}
                     for w in (seg.words or []) if w.end - offset > 0]
            results.append({"start": round(start, 2), "end": round(end, 2), "text": text,
                            "words": words})
        if duration:
            pct = min(100, seg.end / duration * 100)
            print(f"\r  {pct:5.1f}%  [{hms(start)}] {text[:60]:<60}", end="", flush=True)
    print()
    return results, duration


def save_transcript(key: str, media: Path, segments, duration, offset, model_name, device):
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    data = {
        "id": key,
        "source_file": media.name,
        "model": model_name,
        "device": device,
        "duration_seconds": round(duration - offset, 2),
        "offset_seconds": offset,
        "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "segments": segments,
    }
    json_path = TRANSCRIPTS_DIR / f"{key}.json"
    txt_path = TRANSCRIPTS_DIR / f"{key}.txt"
    json_path.write_text(json.dumps(data, indent=2) + "\n")
    txt_path.write_text("".join(f"[{hms(s['start'])}] {s['text']}\n" for s in segments))
    return json_path, txt_path


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe meeting recordings.")
    parser.add_argument("file", nargs="?", type=Path, help="recording to transcribe")
    parser.add_argument("--video-id", help="YouTube video ID from meetings.json")
    parser.add_argument("--offset", type=float, default=0.0, help="seconds of lead-in to trim")
    parser.add_argument("--model", default="large-v3", help="Whisper model name")
    parser.add_argument("--cpu", action="store_true", help="force CPU mode")
    args = parser.parse_args()

    meetings = load_meetings()

    if args.file:
        if not args.file.exists():
            print(f"[!] File not found: {args.file}")
            return 1
        key = args.video_id or args.file.stem
        if args.video_id and args.video_id not in meetings:
            print(f"[!] {args.video_id} isn't in meetings.json; transcribing anyway.")
        jobs = [(args.file, key)]
    else:
        jobs = find_pending_recordings(meetings)
        if not jobs:
            print("Nothing to transcribe. Put recordings in recordings/ named <video_id>.mkv,")
            print("or pass a file directly: python transcribe.py my_recording.mkv")
            return 0

    print(f"Loading Whisper model '{args.model}' (first run downloads it)...")
    model, device = load_model(args.model, args.cpu)
    print(f"Running on {device.upper()}.")

    for media, key in jobs:
        meeting = meetings.get(key, {})
        label = meeting.get("title", media.name)
        prompt = f"{GLOSSARY} {meeting.get('board', '')}".strip()
        print(f"\nTranscribing: {label}")

        started = time.time()
        segments, duration = transcribe(model, media, args.offset, prompt)
        elapsed = time.time() - started
        meeting_length = max(0.0, duration - args.offset)

        json_path, txt_path = save_transcript(key, media, segments, duration,
                                              args.offset, args.model, device)
        speed = meeting_length / elapsed if elapsed else 0
        print(f"  Done: {len(segments)} segments, {hms(meeting_length)} of meeting in "
              f"{hms(elapsed)} ({speed:.1f}x real time)")
        print(f"  Saved {txt_path.relative_to(HERE)} and {json_path.name}")

        if key in meetings:
            meetings[key]["status"] = "transcribed"
            meetings[key]["transcript"] = str(json_path.relative_to(HERE))
            save_meetings(meetings)

    return 0


if __name__ == "__main__":
    ensure_cuda_libs_on_path()
    sys.exit(main())
