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

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [[ ! -x "$CHROME" ]]; then
  print -u2 "Google Chrome n'est pas installé dans /Applications."
  exit 1
fi

mkdir -p "$PROFILE"
exec "$CHROME" \
  --user-data-dir="$PROFILE" \
  --load-extension="$EXTENSION" \
  --no-first-run \
  --no-default-browser-check \
  "$TARGET"
