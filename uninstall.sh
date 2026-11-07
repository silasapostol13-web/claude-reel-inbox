#!/bin/zsh
# Removes what install.sh set up: the background job, the MCP registration, the speech model and
# compiled helpers. Your processed reels (~/ReelInbox) and the iCloud queue are kept unless you pass
# --all. The Claude skill is kept unless you pass --all (you may have edited it).
set -uo pipefail

if [[ -t 1 ]]; then say() { print -P "%F{208}==>%f %B$1%b"; }; else say() { print -r -- "==> $1"; }; fi
note() { print -r -- "    $1"; }

REPO="${0:A:h}"
SUPPORT="$HOME/Library/Application Support/ReelInbox"
# Use the folders install.sh recorded, falling back to its defaults.
cfg() { plutil -extract "$1" raw -o - "$SUPPORT/config.json" 2>/dev/null || print -r -- "$2"; }
INBOX="$(cfg inbox "$HOME/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/ReelInbox")"
OUT="$(cfg output "$HOME/ReelInbox")"
LABEL="${REEL_INBOX_LABEL:-com.$(whoami).reelinbox}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ALL=0; [[ " $* " == *" --all "* ]] && ALL=1
DRY=0; [[ " $* " == *" --dry-run "* ]] && DRY=1   # show what would be moved, move nothing

# Moves to the Trash (recoverable) instead of deleting.
bin_it() {
  [[ -e "$1" ]] || return 0
  if (( DRY )); then note "would move to Trash: $1"; return 0; fi
  { /usr/bin/trash "$1" 2>/dev/null || mv "$1" "$HOME/.Trash/" ; } && note "moved to Trash: $1"
}

say "Background job"
if (( DRY )); then
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && note "would stop $LABEL" || note "not running"
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && note "stopped $LABEL" || note "not running"
fi
bin_it "$PLIST"

say "Claude Code"
if command -v claude >/dev/null; then
  if (( DRY )); then
    claude mcp get reel-inbox >/dev/null 2>&1 && note "would remove MCP server 'reel-inbox'" || note "MCP server is not registered"
  else
    claude mcp remove --scope user reel-inbox >/dev/null 2>&1 && note "removed MCP server 'reel-inbox'" || note "MCP server was not registered"
  fi
fi

say "Model and helpers"
bin_it "$SUPPORT"
bin_it "$REPO/bin"

if (( ALL )); then
  say "Your data (--all)"
  bin_it "$OUT"
  bin_it "$INBOX"
  bin_it "$HOME/.claude/skills/reel-inbox"
else
  note "Kept: $OUT (your processed reels), the iCloud queue, and ~/.claude/skills/reel-inbox."
  note "Run ./uninstall.sh --all to move those to the Trash too."
fi
say "Done. Delete the Send to Claude shortcut on your iPhone if you no longer want it."
