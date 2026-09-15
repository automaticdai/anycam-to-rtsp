#!/usr/bin/env bash
# Build the portable Windows bundle: dist/anycam-windows.zip
#
# Everything under windows/bundle/ ships, verbatim. That is deliberate: this
# script used to cherry-pick `windows/*.ps1`, which silently dropped a helper
# added later because it was not a .ps1 file. A directory cannot forget.
#
# The bundle also carries the anycam package rather than a reimplementation of
# the config generator: the ffmpeg command builder escapes device names for
# MediaMTX's argument parser, and a second implementation would drift from it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/windows/bundle"
STAGE="$(mktemp -d)"
OUT="$ROOT/dist/anycam-windows.zip"
NAME="anycam-windows"

trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME"
cp -r "$BUNDLE"/. "$STAGE/$NAME/"

# The capture half needs the package, but not the consumer's dependencies --
# setup.ps1 installs it with --no-deps plus PyYAML.
mkdir -p "$STAGE/$NAME/src"
cp -r "$ROOT/src/anycam" "$STAGE/$NAME/src/"
cp "$ROOT/pyproject.toml" "$STAGE/$NAME/"
cp "$ROOT/config.example.yaml" "$STAGE/$NAME/"

find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE/$NAME" -name '*.pyc' -delete

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
tick "Add python.exe to PATH"). run.ps1 downloads ffmpeg and MediaMTX
into bin\ and installs nothing system-wide.
TXT

mkdir -p "$ROOT/dist"
rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" "$NAME" )

echo "built: $OUT"
( cd "$STAGE/$NAME" && find . -type f | sed 's|^\./|  |' | sort )
