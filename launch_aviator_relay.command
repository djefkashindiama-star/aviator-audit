#!/bin/zsh
set -eu

ROOT="${0:A:h}"
EXTENSION="$ROOT/browser-relay"
PROFILE="$HOME/.aviator-audit-browser"
TARGET="https://www.premierbet.com/cd/casino/game/aviator-291195"

if [[ ! -f "$EXTENSION/config.js" ]]; then
  print -u2 "Configuration locale absente: $EXTENSION/config.js"
  exit 1
fi

CFT="$HOME/.aviator-audit-runtime/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [[ -x "$CFT" ]]; then
  CHROME="$CFT"
fi
if [[ ! -x "$CHROME" ]]; then
  print -u2 "Aucun navigateur Chrome compatible n'est installé."
  exit 1
fi

mkdir -p "$PROFILE"
exec "$CHROME" \
  --user-data-dir="$PROFILE" \
  --load-extension="$EXTENSION" \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --no-first-run \
  --no-default-browser-check \
  "$TARGET"
