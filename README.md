# local-scribe

A 100% local audio-to-note pipeline for Ubuntu (GNOME/Wayland) and Windows (WSL2).
Press a shortcut to start, a tray icon reminds you it is recording, press another to stop →
Whisper transcribes **on your machine**, Claude writes a clean summary, and the note lands in
your Obsidian vault.

No bots. No cloud audio. No friction.

## The problem it solves

Meeting-notetaker bots join your call, record to someone else's cloud, and feel wrong for 1:1s,
hallway chats, or anything sensitive — and you still end up copy-pasting transcripts by hand.
local-scribe flips that: **no bot joins the meeting**, the **audio never leaves your computer**
(capture and transcription are local), and you get a structured Markdown note in your vault
automatically. Two modes: press-to-record for meetings, and an always-on "ambient" mode that
quietly turns your day's audio into notes.

> The runtime service (daemon, venv, socket, autostart) uses the internal name `meeting-recorder`.

## Prerequisites

- **Ubuntu** (GNOME/Wayland) or **Windows 10/11 with WSL2**. *(macOS not supported yet.)*
- A **summarizer** — pick one (the installer asks you):
  - **Claude Code** already installed (`claude` on your PATH) → nothing else needed (default), or
  - an **Anthropic API key** (the installer prompts for it), or
  - a local **Ollama** model for fully-offline summaries (set it in `.env` afterward).
- Everything else (Python, audio tools, the tray icon) is installed for you. The first recording
  also downloads the Whisper speech model once (~466 MB) — the installer pre-fetches it.

## Install (step by step)

New to the terminal? These lines are the whole thing — paste them one at a time.

```bash
# 1. get the code
git clone <this-repo>
cd local-scribe

# 2. run the installer — it asks one or two questions, then sets everything up and starts it
bash install.sh --start-now
```

The installer asks which summarizer to use (and your API key if you choose that), installs the
dependencies, registers the keyboard shortcuts, pre-downloads the Whisper model, and launches
the tray app. Re-run it any time — it is idempotent (fixes drift, never breaks a working setup).

Your notes land in your Obsidian vault at `~/Documents/obsidian-vault/inbox/` by default. On a
different setup, open the generated `.env` and change `VAULT_PATH`.

## Usage

| Shortcut           | Action                                      |
|--------------------|---------------------------------------------|
| `Super+Shift+R`    | Start recording (tray turns red)            |
| `Super+Shift+S`    | Stop and fire the pipeline in the background |

The final note appears at `$VAULT_PATH/$NOTES_SUBDIR/meeting-YYYY-MM-DD-HHMM.md` (default: `obsidian-vault/inbox/`).
Whisper runs on CPU — a 45-minute meeting takes ~3-5 min to become a note.

## How it works

```
[Super+Shift+R]                                       [Super+Shift+S]
       ↓                                                     ↓
   gsettings → daemon (--start)              gsettings → daemon (--stop)
       ↓                                                     ↓
   pactl module-null-sink "meetingmix"            SIGINT on parecord
   pactl loopback monitor → meetingmix            pactl unload-module ×3
   pactl loopback mic     → meetingmix            spawn pipeline.py (detached)
   parecord meetingmix.monitor → /tmp/...wav             ↓
                                                  whisper small (auto lang)
                                                  claude-sonnet-4-6
                                                  writes .md + notify-send
                                                  deletes the WAV
```

- **Daemon** (`recorder/daemon.py`): the single autostart process. Holds the tray icon (pystray) and listens on a Unix socket at `$XDG_RUNTIME_DIR/meeting-recorder.sock`. The GNOME shortcuts are lightweight clients that send `start`/`stop` over the socket.
- **Pipeline** (`recorder/pipeline.py`): standalone, fire-and-forget. Takes the WAV path. On error it preserves the WAV and logs to `~/.local/state/meeting-recorder/error.log` — reprocessing is manual: `python -m recorder.pipeline /tmp/meeting-recorder/<file>.wav`.

## Configuration (`.env`)

| Variable              | Default                                | What it does                                    |
|-----------------------|----------------------------------------|-------------------------------------------------|
| `VAULT_PATH`          | `~/Documents/obsidian-vault`          | Vault root.                                     |
| `NOTES_SUBDIR`        | `inbox`                                | Subfolder of the vault where the note lands.    |
| `OUTPUT_LANGUAGE`     | `pt`                                   | Language of the generated note. `pt`/`en`/`es` (or any language name). Independent of `WHISPER_LANGUAGE`; drives the summary and its section headings. |
| `WHISPER_MODEL`       | `small`                                | `tiny`/`base`/`small`/`medium`/`large`.         |
| `CLAUDE_MODEL`        | `claude-sonnet-4-6`                    | Model used for summarization.                   |
| `SUMMARIZER_BACKEND`  | `claude_cli`                           | `claude_cli` (uses OAuth/Max via `claude -p`) or `anthropic_api` (uses an API key, billed per-token). |
| `CLAUDE_BIN`          | `claude`                               | Path to the Claude Code CLI. Use an absolute path if autostart cannot see `~/.local/bin`. |
| `ANTHROPIC_API_KEY`   | —                                      | Required **only if** `SUMMARIZER_BACKEND=anthropic_api`. |

## Consuming the notes (optional)

local-scribe's job ends when the note lands in your vault. Optional **agent
skills** in [`skills/`](skills/) help you process those notes day-to-day in the
agent you already use (Claude Code): `process-inbox` (split signal from noise →
daily notes + action items → archive) and `extract-actions` (pull action items
from one note). See [skills/README.md](skills/README.md) to install. Nothing
there is required to run local-scribe.

## Troubleshooting

**Tray icon does not appear**
GNOME 40+ depends on the AppIndicator extension. On Ubuntu it is enabled by default (`ubuntu-appindicators@ubuntu.com`). If you removed it, reinstall via Extensions or:
```bash
sudo apt install gnome-shell-extension-appindicator
gnome-extensions enable ubuntu-appindicators@ubuntu.com
```

**Shortcut does not fire**
Confirm the daemon is running:
```bash
pgrep -fa recorder.daemon
```
If empty: `~/.venvs/meeting-recorder/bin/python -m recorder.daemon &` and then reopen the session so autostart picks it up.

**Recording is silent**
Did you switch headsets after starting the recording? The default sink changed. Stop and restart the recording — the daemon reattaches to the new defaults.

**Pipeline fails but the WAV was saved**
Check `~/.local/state/meeting-recorder/error.log`. Reprocess manually:
```bash
~/.venvs/meeting-recorder/bin/python -m recorder.pipeline /tmp/meeting-recorder/recording-YYYYMMDD-HHMMSS.wav
```

**Want to tear everything down (shortcuts, autostart, venv)**
```bash
gsettings reset org.gnome.settings-daemon.plugins.media-keys custom-keybindings
rm -f ~/.config/autostart/meeting-recorder.desktop
rm -rf ~/.venvs/meeting-recorder
```
