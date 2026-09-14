#!/usr/bin/env bash
# Build the portable Windows bundle: dist/anycam-windows.zip
#
# The bundle carries the anycam package itself rather than a reimplementation
# of the config generator. That is deliberate: the ffmpeg command builder
# escapes device names for MediaMTX's argument parser, and a second
# implementation would drift from it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
OUT="$ROOT/dist/anycam-windows.zip"
NAME="anycam-windows"

trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME"
cp "$ROOT"/windows/*.ps1            "$STAGE/$NAME/"
cp "$ROOT/windows/list_cameras.py"  "$STAGE/$NAME/"
cp "$ROOT/windows/README-WINDOWS.md" "$STAGE/$NAME/README.md"
cp "$ROOT/config.example.yaml"       "$STAGE/$NAME/"
cp "$ROOT/pyproject.toml"            "$STAGE/$NAME/"

mkdir -p "$STAGE/$NAME/src"
cp -r "$ROOT/src/anycam" "$STAGE/$NAME/src/"
find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +

# A minimal README pointer so the archive is self-explanatory when unzipped.
cat > "$STAGE/$NAME/START-HERE.txt" <<'TXT'
anycam-to-rtsp - Windows capture host

1. Open PowerShell in this folder.
2. If scripts are blocked:
       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
3. Run:
       .\run.ps1

That is the whole thing: it sets up, finds your cameras, works out which
MJPEG decoder they accept, and starts streaming. Then read README.md.

Requires Python 3.12+ (https://www.python.org/downloads/windows/,
tick "Add python.exe to PATH"). setup.ps1 downloads ffmpeg and
MediaMTX into bin\ and installs nothing system-wide.
TXT

mkdir -p "$ROOT/dist"
rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" "$NAME" )

echo "built: $OUT"
unzip -l "$OUT" | tail -n +4 | head -20
