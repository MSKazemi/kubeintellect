#!/usr/bin/env bash
# Rebuild the publishable chat-UI artifacts from the recorded webm.
#
# Same split as render_all_kq.sh: recording needs a live cluster, a running Gradio app and
# an LLM budget; rendering needs none of them. `chat-ui-crashloop.webm` is the source of
# record — everything this writes can be thrown away and rebuilt.
#
#   .webm  the recording, real time, 1280x800          (source of record, committed)
#   .mp4   H.264 master for the narrated video/YouTube (derived, not committed)
#   .gif   two README-sized highlights                 (derived, committed — docs inline them)
#   .png   the closing frame, full resolution          (derived, committed — poster/thumbnail)
#
# The two GIFs are highlights, not the whole session: one 60-second GIF of a light-themed
# page lands at 4-5 MB whatever the palette, three times the largest cast GIF. The split at
# 31s is the boundary between the two turns, so neither GIF cuts an answer in half.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="${DIR:-$REPO/scripts/demo/chat-ui}"
SRC="$DIR/chat-ui-crashloop.webm"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -f "$SRC" ] || { echo "no recording at $SRC — run record_chat_ui.py first"; exit 1; }

# 5 fps / 840px / 80 colours keeps each GIF under the 1.71 MB the largest cast GIF costs.
FPS="${FPS:-5}"
WIDTH="${WIDTH:-840}"
COLORS="${COLORS:-80}"
SPLIT="${SPLIT:-31}"          # end of turn 1, start of the write attempt

render_gif () {  # out ss to
  ffmpeg -v error -y -ss "$2" -to "$3" -i "$SRC" \
    -vf "fps=$FPS,scale=$WIDTH:-1:flags=lanczos,palettegen=max_colors=$COLORS:stats_mode=diff" \
    "$TMP/pal.png" || return 1
  ffmpeg -v error -y -ss "$2" -to "$3" -i "$SRC" -i "$TMP/pal.png" \
    -lavfi "fps=$FPS,scale=$WIDTH:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
    "$1" || return 1
  echo "  $(basename "$1")  $(stat -c%s "$1") bytes"
}

rc=0
ffmpeg -v error -y -i "$SRC" -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p \
  -movflags +faststart "$DIR/chat-ui-crashloop.mp4" || rc=1
echo "  chat-ui-crashloop.mp4  $(stat -c%s "$DIR/chat-ui-crashloop.mp4") bytes"
ffmpeg -v error -y -sseof -3 -i "$SRC" -frames:v 1 "$DIR/chat-ui-crashloop.png" || rc=1
echo "  chat-ui-crashloop.png  $(stat -c%s "$DIR/chat-ui-crashloop.png") bytes"
render_gif "$DIR/chat-ui-crashloop.gif" 0 "$SPLIT" || rc=1
render_gif "$DIR/chat-ui-rbac-denied.gif" "$SPLIT" 61 || rc=1
exit "$rc"
