#!/usr/bin/env bash
# Rebuild the reading copies and the GIFs from the kq casts.
#
# Separate from record_all_kq.sh on purpose: recording needs a live cluster and an LLM budget,
# rendering needs neither. The casts are the source of record; everything this writes is a
# build artifact and can be thrown away.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CASTS="${CASTS:-$REPO/scripts/demo/casts-kq}"
TEXT="${TEXT:-$REPO/scripts/demo/transcripts-kq}"
GIFS="${GIFS:-$REPO/scripts/demo/gifs-kq}"
PY="${PY:-$REPO/v4/.venv/bin/python}"

# Readability, not fidelity. A cast of a real session is mostly dead time: the model thinks for
# tens of seconds, then paints an answer in a few hundred milliseconds. Played back at 1x the
# GIF spends its length on a spinner and flashes the content past. --max-frame-ms compresses the
# waiting; --min-frame-ms stops a painted frame from flashing; --speed stretches what is left.
FPS="${FPS:-8}"
FONT_SIZE="${FONT_SIZE:-14}"
SPEED="${SPEED:-0.65}"
MIN_FRAME_MS="${MIN_FRAME_MS:-350}"
MAX_FRAME_MS="${MAX_FRAME_MS:-4000}"
TAIL_HOLD="${TAIL_HOLD:-3.5}"

mkdir -p "$TEXT" "$GIFS"
rc=0
for cast in "$CASTS"/*.cast; do
  [ -e "$cast" ] || { echo "no casts in $CASTS"; exit 1; }
  stem="$(basename "$cast" .cast)"
  # --emulate, not the append-only path: kq repaints in place through rich and
  # prompt_toolkit, so stripping CSI from the byte stream yields overwritten garbage.
  # A terminal emulator is the only thing that can say what was on screen.
  "$PY" "$REPO/scripts/demo/cast_to_text.py" --emulate "$cast" > "$TEXT/$stem.txt" || rc=1
  "$PY" "$REPO/scripts/demo/cast_to_gif.py" "$cast" "$GIFS/$stem.gif" \
    --fps "$FPS" --font-size "$FONT_SIZE" --speed "$SPEED" \
    --min-frame-ms "$MIN_FRAME_MS" --max-frame-ms "$MAX_FRAME_MS" \
    --tail-hold "$TAIL_HOLD" || rc=1
done
exit "$rc"
