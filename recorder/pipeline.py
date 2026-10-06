"""Standalone post-recording pipeline: WAV -> transcript -> summary -> note.

Also accepts .vtt files (WebVTT with speaker tags) as input, skipping Whisper.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import shutil
import subprocess
import sys
import traceback
import wave
from pathlib import Path

import whisper
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "") or None  # None = auto-detect
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
VAULT_PATH = Path(os.environ["VAULT_PATH"]).expanduser()
NOTES_SUBDIR = os.environ.get("NOTES_SUBDIR", "inbox")
SUMMARIZER_BACKEND = os.environ.get("SUMMARIZER_BACKEND", "claude_cli")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Default language for generated notes. pt/en/es (or any key in LANG_NAMES);
# falls back to the given string verbatim if unknown.
OUTPUT_LANGUAGE = os.environ.get("OUTPUT_LANGUAGE", "pt")
STATE_DIR = Path.home() / ".local/state/meeting-recorder"
_archive_raw = os.environ.get("RECORDINGS_ARCHIVE_DIR", "")
RECORDINGS_ARCHIVE_DIR: Path | None = Path(_archive_raw).expanduser() if _archive_raw else None

LANG_NAMES = {"pt": "Portuguese (Brazilian)", "en": "English", "es": "Spanish"}


def output_language_name() -> str:
    """Human-readable name of the configured output language for prompts."""
    return LANG_NAMES.get(OUTPUT_LANGUAGE, OUTPUT_LANGUAGE)


# Short localized labels for the section headings that Python emits directly
# (outside the LLM-generated note body). Keyed by OUTPUT_LANGUAGE. The LLM note
# produces its own section headings in the target language via the prompt; these
# cover the trailing "Full Transcript" heading and the ambient "Audio Sources"
# heading, which are written without an LLM call.
_FULL_TRANSCRIPT_LABEL = {"en": "Full Transcript", "es": "Transcripción Completa"}  # OUTPUT_LANGUAGE
_FULL_TRANSCRIPT_LABEL["pt"] = "Transcrição Completa"  # OUTPUT_LANGUAGE=pt

_AUDIO_SOURCES_LABEL = {"en": "Audio Sources", "es": "Fuentes de Audio"}  # OUTPUT_LANGUAGE
_AUDIO_SOURCES_LABEL["pt"] = "Fontes de Áudio"  # OUTPUT_LANGUAGE=pt


def full_transcript_heading() -> str:
    """Localized 'Full Transcript' heading for the configured OUTPUT_LANGUAGE."""
    return _FULL_TRANSCRIPT_LABEL.get(OUTPUT_LANGUAGE, _FULL_TRANSCRIPT_LABEL["en"])


def audio_sources_heading() -> str:
    """Localized 'Audio Sources' heading for the configured OUTPUT_LANGUAGE."""
    return _AUDIO_SOURCES_LABEL.get(OUTPUT_LANGUAGE, _AUDIO_SOURCES_LABEL["en"])


SYSTEM_PROMPT = """\
You receive the raw transcription of a meeting (auto-transcribed via Whisper — may contain \
ASR errors, wrong names, mangled technical terms). You have NO other context about the participants \
or projects. Because of this, your job is to be a FAITHFUL and DETAILED scribe, not an editor: \
err heavily on the side of including too much rather than too little. If something was mentioned \
in passing but could matter, include it. The reader will later cross-reference with their own context \
to decide what's relevant.

Write the note in {lang_name}. EVERY part of the note — all section headings and all fallback \
sentences — must be written in {lang_name}. Do not emit any heading or text in another language.

Produce a structured note as Markdown with EXACTLY these six sections, in this order. \
The heading labels below are given in English only to define the required structure; translate \
each heading into {lang_name} in your output:

1. Executive Summary
3–5 sentences. Who was in the meeting (list all names/roles mentioned), what it was about, \
and what the main outcome was. If names are unclear due to ASR errors, write your best guess \
followed by [?].

2. Topics Discussed
A topic-by-topic breakdown of everything covered. For each distinct subject, write a **bold heading** \
followed by 3–8 bullet points capturing what was said in detail. Aim for specificity: \
numbers, names, decisions-in-progress, differing opinions — capture it all. \
Do not merge topics that were discussed separately.

3. Points of Attention
Risks, ambiguities, open questions, blockers, or tensions that were raised (explicitly or implicitly). \
One bullet per point, with enough context to be actionable. \
If none, write a single sentence in {lang_name} stating that no point of attention was identified.

4. Decisions
Bulleted list of decisions that were clearly made. One bullet per decision. \
If none, write a single sentence in {lang_name} stating that no decision was recorded.

5. Action Items
Markdown checkboxes. Format: `- [ ] <action> (@owner)`. Omit @owner if not mentioned. \
Include every commitment made, even vague ones (e.g. "will check", "will send by email"). \
If none, write a single sentence in {lang_name} stating that no action item was recorded.

6. Next Steps
Follow-ups, scheduled events, or things explicitly flagged as "to revisit". \
If none, write a single sentence in {lang_name} stating that no next step was recorded.

Each section heading must be a level-2 Markdown heading (`## ...`), translated into {lang_name}. \
Output ONLY the six sections, starting with the translated Executive Summary heading. \
No preamble, no closing remarks, no code fences.\
"""


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes}min" if minutes else f"{int(seconds)}s"


def notify(title: str, body: str) -> None:
    if sys.platform == "win32":
        try:
            from plyer import notification
            notification.notify(app_name="Meeting Recorder", title=title, message=body, timeout=5)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["notify-send", "-a", "Meeting Recorder", title, body], check=False)
        except FileNotFoundError:
            pass


def log_error(wav_path: Path, exc: BaseException) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = STATE_DIR / "error.log"
    with log.open("a") as f:
        f.write(f"\n--- {dt.datetime.now().isoformat()} {wav_path} ---\n")
        f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def parse_vtt(vtt_path: Path) -> tuple[str, float]:
    """Parse WebVTT with speaker tags into a readable transcript. Returns (text, duration_seconds)."""
    text = vtt_path.read_text(errors="replace")

    # Extract all cues: (timestamp_end_seconds, speaker, content)
    cue_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d+)\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d+)\n(.*?)(?=\n\n|\Z)",
        re.DOTALL,
    )

    def ts_to_seconds(ts: str) -> float:
        ts = ts.replace(",", ".")
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    speaker_re = re.compile(r"<v ([^>]+)>(.*?)(?:</v>|$)", re.DOTALL)

    segments: list[tuple[str, str]] = []  # (speaker, text)
    max_end = 0.0

    for m in cue_re.finditer(text):
        end_s = ts_to_seconds(m.group(2))
        max_end = max(max_end, end_s)
        body = m.group(3).strip()
        sm = speaker_re.search(body)
        if sm:
            speaker = html.unescape(sm.group(1).strip())
            content = html.unescape(re.sub(r"<[^>]+>", "", sm.group(2))).strip()
        else:
            speaker = ""
            content = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()

        if not content:
            continue
        # Merge consecutive lines from same speaker
        if segments and segments[-1][0] == speaker:
            segments[-1] = (speaker, segments[-1][1] + " " + content)
        else:
            segments.append((speaker, content))

    lines = [f"{spk}: {txt}" if spk else txt for spk, txt in segments]
    return "\n".join(lines), max_end


def transcribe(wav_path: Path) -> tuple[str, str]:
    import numpy as np
    with wave.open(str(wav_path), "rb") as w:
        frames = w.readframes(w.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    model = whisper.load_model(WHISPER_MODEL)
    result = model.transcribe(audio, language=WHISPER_LANGUAGE, fp16=False)
    return result["text"].strip(), result["language"]


def summarize(transcript: str, language: str) -> str:
    if SUMMARIZER_BACKEND == "claude_cli":
        return _summarize_via_cli(transcript, language)
    if SUMMARIZER_BACKEND == "anthropic_api":
        return _summarize_via_api(transcript, language)
    if SUMMARIZER_BACKEND == "ollama":
        return _summarize_via_ollama(transcript, language)
    raise ValueError(
        f"Unknown SUMMARIZER_BACKEND={SUMMARIZER_BACKEND!r}; expected 'claude_cli', 'anthropic_api' or 'ollama'"
    )


def _build_prompt(transcript: str, language: str) -> str:
    # `language` is the transcript's detected language; the note's output language
    # is driven by OUTPUT_LANGUAGE instead.
    lang_name = output_language_name()
    return (
        f"{SYSTEM_PROMPT.format(lang_name=lang_name)}\n\n"
        "---\n\n"
        f"{transcript}"
    )


def _summarize_via_cli(transcript: str, language: str) -> str:
    claude_path = shutil.which(CLAUDE_BIN)
    if claude_path is None:
        raise RuntimeError(
            f"'{CLAUDE_BIN}' not found in PATH. Install Claude Code or set CLAUDE_BIN in .env."
        )
    prompt = _build_prompt(transcript, language)
    result = subprocess.run(
        [claude_path, "-p", "--model", CLAUDE_MODEL],
        input=prompt,
        capture_output=True, text=True, check=True, timeout=600,
    )
    return result.stdout.strip()


def _summarize_via_ollama(transcript: str, language: str) -> str:
    import json
    import urllib.request
    prompt = _build_prompt(transcript, language)
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return data["message"]["content"].strip()


def _summarize_via_api(transcript: str, language: str) -> str:
    import anthropic
    lang_name = output_language_name()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT.format(lang_name=lang_name),
        messages=[{"role": "user", "content": _build_prompt(transcript, language)}],
    )
    return response.content[0].text.strip()


def write_note(
    summary_md: str,
    transcript: str,
    *,
    started_at: dt.datetime,
    duration_s: float,
    language: str,
    source: str = "whisper",
) -> Path:
    notes_dir = VAULT_PATH / NOTES_SUBDIR
    notes_dir.mkdir(parents=True, exist_ok=True)
    fname = f"meeting-{started_at.strftime('%Y-%m-%d-%H%M')}.md"
    path = notes_dir / fname
    frontmatter = (
        "---\n"
        f"date: {started_at.strftime('%Y-%m-%d')}\n"
        f"duration: {format_duration(duration_s)}\n"
        f"language: {language}\n"
        "tags: [meeting]\n"
        "---\n\n"
        f"# Meeting – {started_at.strftime('%Y-%m-%d %H:%M')}\n\n"
    )
    if source == "vtt":
        transcription_note = "Transcript source: WebVTT (platform captions)"
    else:
        transcription_note = f"Transcribed with Whisper {WHISPER_MODEL}"
    footer = (
        f"\n\n---\n*{transcription_note} | "
        f"Summarized with {CLAUDE_MODEL} via {SUMMARIZER_BACKEND}*\n\n"
        f"## {full_transcript_heading()}\n\n"
        f"{transcript}\n"
    )
    path.write_text(frontmatter + summary_md + footer, encoding="utf-8")
    return path


def run(wav_path: Path) -> Path:
    duration_s = wav_duration_seconds(wav_path)
    finished_at = dt.datetime.fromtimestamp(wav_path.stat().st_mtime)
    started_at = finished_at - dt.timedelta(seconds=duration_s)

    transcript, language = transcribe(wav_path)
    summary_md = summarize(transcript, language)
    note_path = write_note(
        summary_md, transcript,
        started_at=started_at, duration_s=duration_s, language=language,
    )
    return note_path


def write_ambient_note(
    transcript: str,
    *,
    started_at: dt.datetime,
    duration_s: float,
    language: str,
    audio_log: list | None = None,
) -> Path:
    notes_dir = VAULT_PATH / NOTES_SUBDIR
    notes_dir.mkdir(parents=True, exist_ok=True)
    fname = f"ambient-{started_at.strftime('%Y-%m-%d-%H%M')}.md"
    path = notes_dir / fname

    sources_section = ""
    if audio_log:
        lines = "\n".join(f"- {e['time']} — {', '.join(e['apps'])}" for e in audio_log)
        sources_section = f"\n## {audio_sources_heading()}\n\n{lines}\n"

    content = (
        "---\n"
        f"date: {started_at.strftime('%Y-%m-%d')}\n"
        f"time: {started_at.strftime('%H:%M')}\n"
        f"duration: {format_duration(duration_s)}\n"
        f"language: {language}\n"
        "tags: [ambient]\n"
        "---\n\n"
        f"# Ambient – {started_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"{sources_section}\n"
        f"{transcript}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def run_ambient(wav_path: Path) -> Path | None:
    import json
    duration_s = wav_duration_seconds(wav_path)
    finished_at = dt.datetime.fromtimestamp(wav_path.stat().st_mtime)
    started_at = finished_at - dt.timedelta(seconds=duration_s)
    transcript, language = transcribe(wav_path)
    if not transcript.strip():
        return None
    sidecar = wav_path.with_suffix(".sources.json")
    audio_log = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None
    if sidecar.exists():
        try:
            sidecar.unlink()
        except OSError:
            pass
    return write_ambient_note(
        transcript,
        started_at=started_at, duration_s=duration_s, language=language,
        audio_log=audio_log,
    )


def run_from_vtt(vtt_path: Path) -> Path:
    transcript, duration_s = parse_vtt(vtt_path)
    # Use file mtime as end time; vtt files from meeting platforms are usually saved right after
    finished_at = dt.datetime.fromtimestamp(vtt_path.stat().st_mtime)
    started_at = finished_at - dt.timedelta(seconds=duration_s)
    summary_md = summarize(transcript, "en")
    note_path = write_note(
        summary_md, transcript,
        started_at=started_at, duration_s=duration_s, language="en",
        source="vtt",
    )
    return note_path


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="WAV or VTT file to process")
    parser.add_argument("--ambient", action="store_true", help="Ambient mode: save raw transcript only, no summary")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"File not found: {input_path}", file=sys.stderr)
        return 1

    is_vtt = input_path.suffix.lower() == ".vtt"

    try:
        if args.ambient:
            note_path = run_ambient(input_path)
        elif is_vtt:
            note_path = run_from_vtt(input_path)
        else:
            note_path = run(input_path)
    except Exception as exc:
        log_error(input_path, exc)
        notify("Meeting pipeline failed", f"See {STATE_DIR}/error.log")
        return 1

    if note_path is None:
        try:
            input_path.unlink()
        except OSError:
            pass
        return 0

    if not args.ambient:
        notify("Meeting saved", note_path.name)
    if not is_vtt:
        if RECORDINGS_ARCHIVE_DIR is not None:
            RECORDINGS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            dest = RECORDINGS_ARCHIVE_DIR / input_path.name
            shutil.move(str(input_path), dest)
        else:
            try:
                input_path.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
