#!/usr/bin/env bash
# Install ffmpeg/ffprobe and the DejaVu fonts the renderer and mock
# providers use. apt first (bounded, so a mirror outage can't hang the job),
# then prebuilt files from GitHub releases for whatever is still missing.
set -uo pipefail

FONT_DIR=/usr/share/fonts/truetype/dejavu
have_all() {
  command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null && [ -f "$FONT_DIR/DejaVuSans-Bold.ttf" ]
}

if ! have_all; then
  APT="-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20"
  timeout 240 sudo apt-get $APT update || echo "apt update failed or timed out"
  timeout 240 sudo apt-get $APT install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    || echo "apt install failed or timed out; falling back to GitHub release downloads"
fi

fetch() {  # url dest
  for attempt in 1 2 3; do
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 300 -o "$2" "$1" && return 0
    echo "download attempt $attempt of $1 failed"; sleep 10
  done
  return 1
}

tmp=$(mktemp -d)
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "installing a static ffmpeg build"
  fetch https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz "$tmp/ffmpeg.tar.xz" || exit 1
  tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp"
  sudo install -m 0755 "$tmp"/ffmpeg-*/bin/ffmpeg "$tmp"/ffmpeg-*/bin/ffprobe /usr/local/bin/
fi
if [ ! -f "$FONT_DIR/DejaVuSans-Bold.ttf" ]; then
  echo "installing the DejaVu fonts"
  fetch https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2 "$tmp/dejavu.tar.bz2" || exit 1
  tar -xjf "$tmp/dejavu.tar.bz2" -C "$tmp"
  sudo mkdir -p "$FONT_DIR"
  sudo cp "$tmp"/dejavu-fonts-ttf-2.37/ttf/*.ttf "$FONT_DIR/"
  command -v fc-cache >/dev/null && sudo fc-cache -f >/dev/null || true
fi
rm -rf "$tmp"

ffmpeg -hide_banner -version | head -1
ffprobe -hide_banner -version | head -1
ls "$FONT_DIR"/DejaVuSans.ttf "$FONT_DIR"/DejaVuSans-Bold.ttf
