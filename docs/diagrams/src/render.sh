#!/usr/bin/env sh
# Render every diagram source to its committed PNG at 2x. Requires Google Chrome.
# Usage: sh docs/diagrams/src/render.sh   (or: make diagrams, which also runs check_counts.py)
set -eu
cd "$(dirname "$0")"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
render() { # name width height
  "$CHROME" --headless --disable-gpu --hide-scrollbars --virtual-time-budget=12000 \
    --screenshot="../$1.png" --window-size="$2,$3" --force-device-scale-factor=2 \
    "file://$PWD/$1.html" 2>/dev/null
  echo "rendered ../$1.png ($2x$3 @2x)"
}
render architecture-overview 1640 510
render investigation-graph   1680 512
render data-model            1900 539
render technical-architecture 1900 840
