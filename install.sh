#!/usr/bin/env bash
# Idempotent bootstrap for meeting-recorder. Safe to run repeatedly.
# Usage: bash install.sh [--start-now]

set -euo pipefail

START_NOW=0
for arg in "$@"; do
  case "$arg" in
    --start-now) START_NOW=1 ;;
    -h|--help)
      echo "Usage: bash install.sh [--start-now]"
      exit 0
      ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.venvs/meeting-recorder"
AUTOSTART_DIR="${HOME}/.config/autostart"
DESKTOP_DEST="${AUTOSTART_DIR}/meeting-recorder.desktop"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; DIM='\033[2m'; RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${RESET} %s\n" "$*"; }
fail() { printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit 1; }

# ── 1. apt deps ──────────────────────────────────────────────────────────────
say "Installing apt dependencies (one auth prompt — sudo or pkexec popup)..."
APT_PKGS=(
  pipewire pipewire-pulse wireplumber pulseaudio-utils
  ffmpeg libnotify-bin
  python3 python3-venv python3-pip python3-gi
  gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1
)

# Single root invocation so we only authenticate once. Falls back across sudo (cached),
# pkexec (GUI popup, works without TTY), and interactive sudo.
ROOT_SCRIPT="$(mktemp /tmp/meeting-recorder-apt.XXXXXX.sh)"
cat > "${ROOT_SCRIPT}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y ${APT_PKGS[*]}
EOF
chmod 755 "${ROOT_SCRIPT}"

if sudo -n true 2>/dev/null; then
  sudo bash "${ROOT_SCRIPT}"
elif command -v pkexec >/dev/null && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  pkexec bash "${ROOT_SCRIPT}"
else
  sudo bash "${ROOT_SCRIPT}"
fi
rm -f "${ROOT_SCRIPT}"

# ── 2. virtualenv + python deps ──────────────────────────────────────────────
# Venv must use --system-site-packages so it sees the system python3-gi
# (PyGObject), which pystray needs for the AppIndicator tray backend on
# GNOME/Wayland. Recreate if an existing venv lacks gi.
needs_venv=0
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  needs_venv=1
elif ! "${VENV_DIR}/bin/python" -c "import gi" 2>/dev/null; then
  warn "Existing venv lacks 'gi' — recreating with --system-site-packages"
  rm -rf "${VENV_DIR}"
  needs_venv=1
fi
if [[ "${needs_venv}" -eq 1 ]]; then
  say "Creating venv at ${VENV_DIR} (--system-site-packages)"
  python3 -m venv --system-site-packages "${VENV_DIR}"
else
  say "Venv already present at ${VENV_DIR}"
fi
say "Installing Python requirements (no-op if satisfied)"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

# ── 3. .env bootstrap ────────────────────────────────────────────────────────
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  cp "${REPO_DIR}/config.example.env" "${REPO_DIR}/.env"
  warn "${REPO_DIR}/.env created from template. Edit it and set ANTHROPIC_API_KEY."
else
  say ".env already present"
fi

# ── 4. GNOME custom keyboard shortcuts (idempotent) ──────────────────────────
say "Registering GNOME shortcuts (Super+Shift+R / Super+Shift+S)"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
KB_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KB_BASE="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
KB_START_PATH="${KB_BASE}/meeting-recorder-start/"
KB_STOP_PATH="${KB_BASE}/meeting-recorder-stop/"

current=$(gsettings get "${SCHEMA}" custom-keybindings)
new="${current}"
for kb in "${KB_START_PATH}" "${KB_STOP_PATH}"; do
  if [[ "${new}" != *"'${kb}'"* ]]; then
    if [[ "${new}" == "@as []" ]] || [[ "${new}" == "[]" ]]; then
      new="['${kb}']"
    else
      new="${new%]*}, '${kb}']"
    fi
  fi
done
if [[ "${new}" != "${current}" ]]; then
  gsettings set "${SCHEMA}" custom-keybindings "${new}"
fi

# PYTHONPATH is needed because GNOME spawns the keybinding from an arbitrary cwd
# where `python -m recorder.daemon` can't find the package.
gsettings set "${KB_SCHEMA}:${KB_START_PATH}" name "Meeting Recorder — Start"
gsettings set "${KB_SCHEMA}:${KB_START_PATH}" command "env PYTHONPATH=${REPO_DIR} ${VENV_DIR}/bin/python -m recorder.daemon --start"
gsettings set "${KB_SCHEMA}:${KB_START_PATH}" binding "<Super><Shift>r"

gsettings set "${KB_SCHEMA}:${KB_STOP_PATH}" name "Meeting Recorder — Stop"
gsettings set "${KB_SCHEMA}:${KB_STOP_PATH}" command "env PYTHONPATH=${REPO_DIR} ${VENV_DIR}/bin/python -m recorder.daemon --stop"
gsettings set "${KB_SCHEMA}:${KB_STOP_PATH}" binding "<Super><Shift>s"

# ── 5. autostart .desktop ────────────────────────────────────────────────────
say "Installing autostart entry at ${DESKTOP_DEST}"
mkdir -p "${AUTOSTART_DIR}"
sed \
  -e "s|__VENV_PYTHON__|${VENV_DIR}/bin/python|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  "${REPO_DIR}/autostart/meeting-recorder.desktop" > "${DESKTOP_DEST}"
chmod +x "${DESKTOP_DEST}"

# ── 6. final checks ──────────────────────────────────────────────────────────
say "Verification:"
printf "  venv:           %s\n" "${VENV_DIR}"
printf "  autostart:      %s\n" "${DESKTOP_DEST}"
printf "  shortcuts:      Super+Shift+R (start) / Super+Shift+S (stop)\n"

# Only warn about ANTHROPIC_API_KEY if the user actually selected the API backend.
backend="$(grep -E '^SUMMARIZER_BACKEND=' "${REPO_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
backend="${backend:-claude_cli}"
if [[ "${backend}" == "anthropic_api" ]] && grep -q '^ANTHROPIC_API_KEY=$' "${REPO_DIR}/.env" 2>/dev/null; then
  warn "ANTHROPIC_API_KEY is empty in .env and SUMMARIZER_BACKEND=anthropic_api — pipeline will fail."
fi
if [[ "${backend}" == "claude_cli" ]] && ! command -v claude >/dev/null 2>&1; then
  warn "SUMMARIZER_BACKEND=claude_cli but 'claude' is not in PATH. Install Claude Code or set CLAUDE_BIN in .env."
fi

# Check Ubuntu AppIndicator extension status (warning only — Ubuntu enables by default).
if command -v gnome-extensions >/dev/null 2>&1; then
  if gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
    printf "  appindicator:   ${GREEN}enabled${RESET}\n"
  else
    warn "AppIndicator GNOME extension not enabled — tray icon may not appear."
    warn "Enable 'Ubuntu AppIndicators' or 'AppIndicator and KStatusNotifierItem Support' in Extensions."
  fi
fi

if [[ "${START_NOW}" -eq 1 ]]; then
  if pgrep -f "recorder.daemon$" >/dev/null; then
    say "Daemon already running"
  else
    say "Starting daemon now (detached)"
    nohup "${VENV_DIR}/bin/python" -m recorder.daemon \
      </dev/null >/dev/null 2>&1 &
    disown || true
  fi
fi

say "Done. Re-run anytime — this script is idempotent."
