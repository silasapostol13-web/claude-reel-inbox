#!/bin/zsh
# Reel Inbox installer. Safe to run again any time (after an update, or if you move the folder).
#
#   1. installs the free tools it needs with Homebrew (python, yt-dlp, ffmpeg, whisper-cpp)
#   2. downloads the local speech model once (~575 MB)
#   3. compiles two small Swift helpers (Apple Vision OCR, iCloud file reader)
#   4. starts a background job (launchd) that watches the iCloud inbox
#   5. registers the MCP server for every Claude Code session, and adds a small Claude skill
#
# Undo everything with ./uninstall.sh
set -euo pipefail

# Colors only in a real terminal; plain text when piped or logged.
if [[ -t 1 ]]; then
  say()  { print -P "%F{208}==>%f %B$1%b"; }
  note() { print -P "    $1"; }
  fail() { print -P "%F{red}✗%f $1"; exit 1; }
else
  say()  { print -r -- "==> $1"; }
  note() { local s="${1//\%F\{yellow\}/}"; print -r -- "    ${s//\%f/}"; }
  fail() { print -r -- "x $1"; exit 1; }
fi

REPO="${0:A:h}"
SUPPORT="$HOME/Library/Application Support/ReelInbox"
# Shortcuts' iCloud Drive folder. The ID is Apple's and the same on every Mac.
SHORTCUTS_ICLOUD="$HOME/Library/Mobile Documents/iCloud~is~workflow~my~workflows"
INBOX="${REEL_INBOX_SRC:-$SHORTCUTS_ICLOUD/Documents/ReelInbox}"   # where the phone drops shares
OUT="${REEL_INBOX_OUT:-$HOME/ReelInbox}"                             # where processed reels go
MODEL="$SUPPORT/models/ggml-large-v3-turbo-q5_0.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
LABEL="${REEL_INBOX_LABEL:-com.$(whoami).reelinbox}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[[ "$(uname)" == "Darwin" ]] || fail "Reel Inbox runs on macOS only."

# --- 1. tools -------------------------------------------------------------------------------
say "Checking tools"
command -v brew >/dev/null || fail "Homebrew is needed. Install it from https://brew.sh, then run ./install.sh again."
BREW_PREFIX="$(brew --prefix)"
xcrun --find swiftc >/dev/null 2>&1 || fail "Xcode Command Line Tools are needed for swiftc. Run: xcode-select --install"

# Python: any Homebrew python3 is fine (stdlib only). Don't install a new one on top of yours.
missing=()
[[ -x "$BREW_PREFIX/bin/python3" ]] || missing+=(python)
for formula in yt-dlp ffmpeg whisper-cpp; do
  brew list --formula "$formula" >/dev/null 2>&1 || missing+=("$formula")
done
if (( ${#missing} )); then
  note "Installing with Homebrew: ${missing[*]}"
  brew install "${missing[@]}"
else
  note "python3, yt-dlp, ffmpeg, whisper-cpp: already installed"
fi
PY="$BREW_PREFIX/bin/python3"
[[ -x "$PY" ]] || fail "Couldn't find Homebrew's python3 at $PY"

# --- 2. speech model ------------------------------------------------------------------------
say "Speech model"
mkdir -p "$SUPPORT/models" "$OUT"
if [[ -s "$MODEL" ]]; then
  note "already downloaded"
else
  note "downloading whisper large-v3-turbo (q5, ~575 MB, one time)"
  curl -L --fail --progress-bar -o "$MODEL.part" "$MODEL_URL" && mv "$MODEL.part" "$MODEL"
fi

# --- 3. Swift helpers -----------------------------------------------------------------------
say "Compiling helpers"
mkdir -p "$REPO/bin"
swiftc -O "$REPO/vision/reelvision.swift" -o "$REPO/bin/reelvision"
swiftc -O "$REPO/vision/icloudread.swift" -o "$REPO/bin/icloudread"
note "bin/reelvision, bin/icloudread"

# --- 4. background job ----------------------------------------------------------------------
say "Background job"
if [[ ! -d "$SHORTCUTS_ICLOUD" ]]; then
  note "%F{yellow}Heads up:%f the Shortcuts iCloud folder doesn't exist yet. Make sure iCloud Drive is on"
  note "(System Settings > Apple Account > iCloud > iCloud Drive). It will work once the first share arrives."
fi
mkdir -p "$INBOX" "$HOME/Library/LaunchAgents"
# Settings the worker and MCP server read (an environment variable still overrides each one).
"$PY" -c 'import json,sys; json.dump(dict(zip(["inbox","output","model"], sys.argv[1:])), open(sys.argv[4],"w"), indent=2)' \
  "$INBOX" "$OUT" "$MODEL" "$SUPPORT/config.json"
note "settings: $SUPPORT/config.json (reels go to $OUT)"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$REPO/worker/reel_worker.py</string></array>
  <key>WatchPaths</key><array><string>$INBOX</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StandardOutPath</key><string>$SUPPORT/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$SUPPORT/launchd.err.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
note "running as $LABEL (checks the inbox on change, at login, and every 2 minutes)"

# --- 5. Claude Code -------------------------------------------------------------------------
say "Claude Code"
SKILL_DIR="$HOME/.claude/skills/reel-inbox"
if [[ -f "$SKILL_DIR/SKILL.md" ]]; then
  note "skill already present, left as is: $SKILL_DIR"
else
  mkdir -p "$SKILL_DIR" && cp "$REPO/skill/reel-inbox/SKILL.md" "$SKILL_DIR/SKILL.md"
  note "added skill: $SKILL_DIR"
fi
if command -v claude >/dev/null; then
  claude mcp remove --scope user reel-inbox >/dev/null 2>&1 || true
  claude mcp add --scope user reel-inbox -- "$PY" "$REPO/mcp/reel_mcp.py" >/dev/null
  note "MCP server 'reel-inbox' registered for every session (user scope)"
else
  note "%F{yellow}Claude Code CLI not found.%f Install it (https://docs.anthropic.com/en/docs/claude-code),"
  note "then run ./install.sh again to register the MCP server."
fi

say "Done"
note "Next: put shortcut/Send to Claude.shortcut on your iPhone (see README, step 4)."
note "Test without the phone:  python3 \"$REPO/worker/reel_worker.py\" --url <reel link> --note test"
