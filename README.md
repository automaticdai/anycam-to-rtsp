# anycam-to-rtsp

Streams Windows USB cameras into WSL2 for real-time computer vision, keeping
drivers on Windows and always delivering the freshest available frame.

## How it works

    USB cam ──DirectShow──▶ ffmpeg ──NVENC h264──▶ MediaMTX /camN
                                                        │ RTSP/TCP
                              WSL: PyAV ──NVDEC──▶ newest-frame buffer ──▶ inference

One ffmpeg process, one RTSP path, one receiver thread and one watchdog per
camera. Nothing is shared between cameras, so one failing camera cannot affect
the others.

Frames are dropped rather than queued. A consumer slower than the stream sees a
lower frame rate, never growing latency.

## Setup

**Windows** — install [ffmpeg](https://ffmpeg.org/download.html) and
[MediaMTX](https://github.com/bluenviron/mediamtx/releases), then:

    anycam devices                      # find your camera names
    anycam serve-config -c config.yaml  # writes mediamtx.yml
    mediamtx.exe mediamtx.yml

**WSL** — no `sudo` required. Both binaries can be installed as plain user
binaries on `PATH` (e.g. `~/.local/bin`):

    # ffmpeg: static build, no package manager needed
    curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
      | tar -xJ --strip-components=1 -C ~/.local/bin --wildcards '*/ffmpeg' '*/ffprobe'

    # mediamtx: GitHub release tarball (only needed in WSL to run the
    # integration test suite against a real binary; production MediaMTX runs
    # on Windows, see above)
    curl -L https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_amd64.tar.gz \
      | tar -xz -C ~/.local/bin mediamtx

    pip install -e ".[dev]"
    anycam watch -c config.yaml

Versions verified in this environment: ffmpeg 7.0.2-static, MediaMTX v1.9.3,
PyAV 18.1.0. Installing via a package manager (`apt install ffmpeg`, plus a
manually-downloaded MediaMTX binary) works too and is the more common path on
a persistent machine, but was not the one exercised here — this repo's own
verification used the no-sudo, user-local install above.

## Usage

```python
from anycam.client import MultiCameraClient
from anycam.config import load_config
from anycam.host import discover_windows_host

config = load_config("config.yaml")
with MultiCameraClient(config, discover_windows_host()) as client:
    while True:
        frame = client.take("cam0")
        if frame is not None:
            run_inference(frame.image)
```

## Testing

    pytest tests -m "not integration"   # no hardware needed
    pytest tests -m integration         # needs ffmpeg + mediamtx, no camera

Hardware verification is documented in [docs/bench-checklist.md](docs/bench-checklist.md).

## Latency calibration

No in-band timestamp can measure glass-to-inference latency: a capture
timestamp is taken when a frame reaches software, already tens of
milliseconds after light hit the sensor. `tools/calibrate_latency.py` measures
the whole chain — exposure, USB transfer, encode, transport and decode — the
only way that is possible: by filming a millisecond clock and reading the
digits off the saved frames against the WSL arrival time.

Reading the digits is deliberately manual. Automating it with OCR would add a
dependency and a failure mode to a procedure that only runs a handful of
times per hardware change.

The `measure` subcommand needs OpenCV to burn the arrival timestamp into the
saved frame, which is why it is an optional extra rather than a base
dependency — a WSL client that only consumes frames never needs it:

    pip install -e ".[calibration]"
    python tools/calibrate_latency.py display                          # on Windows
    python tools/calibrate_latency.py measure -c config.yaml --camera cam0  # in WSL

If `measure` fails with `ModuleNotFoundError: No module named 'cv2'`, that
extra has not been installed.

## Design

See [the design spec](docs/superpowers/specs/2026-09-14-anycam-to-rtsp-design.md)
for why H.264 rather than MJPEG, why the watchdog is not redundant with
reconnect, and why latency is measured out of band.
