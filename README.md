# anycam-to-rtsp

Streams Windows USB cameras into WSL2 for real-time computer vision, keeping
drivers on Windows and always delivering the freshest available frame.

## How it works

    USB cam ──DirectShow──▶ ffmpeg ──NVENC h264──▶ MediaMTX /camN
    (Windows)                                           │ RTSP over TCP
                                    WSL: PyAV decode ───┴──▶ newest-frame buffer ──▶ inference

One ffmpeg process, one RTSP path, one receiver thread and one watchdog per
camera. Nothing is shared between cameras, so one failing camera cannot affect
the others.

**Frames are dropped rather than queued.** A consumer slower than the stream
sees a lower frame rate, never growing latency. This is the property the whole
design exists to provide — see *Reading the stats* below, because a healthy
system reports a large and growing drop count.

Encoding is on the GPU. Decoding in WSL is currently on the CPU: PyAV is opened
without a hardware accelerator, which is cheap for one or two 1080p30 streams
and is the documented fallback when NVDEC is not exposed. Requesting NVDEC is a
possible optimisation, not something this code does today.

## Windows side

Everything that runs on the camera machine lives in
[`windows/bundle/`](windows/bundle/) and ships verbatim. Build a portable
archive with:

    ./windows/build-zip.sh        # -> dist/anycam-windows.zip

Copy it to the Windows machine, unzip, open PowerShell in that folder, and:

    .\run.ps1

That is the whole thing: it sets up on first run (venv, ffmpeg, MediaMTX v1.9.3
into `bin\`), detects your cameras, writes `config.yaml` from their real names,
probes which MJPEG decoder they actually accept, generates `mediamtx.yml`, and
starts the server. Only Python 3.12+ is required on Windows — the bundle
installs PyYAML alone, not the video stack.

Check a camera from a browser **on that machine**: <http://localhost:8889/cam0>.
If the picture is there, capture and encoding work, and anything failing in WSL
is networking.

See [windows/bundle/README.md](windows/bundle/README.md) for the individual
scripts, the firewall and mirrored-networking notes, and troubleshooting —
including `CUDA_ERROR_NO_DEVICE`, which can appear on a machine with a perfectly
healthy NVIDIA GPU.

## WSL side

No `sudo` required:

    # ffmpeg and mediamtx are only needed to run the integration suite against
    # real binaries; the production server runs on Windows.
    curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
      | tar -xJ --strip-components=1 -C ~/.local/bin --wildcards '*/ffmpeg' '*/ffprobe'
    curl -L https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_amd64.tar.gz \
      | tar -xz -C ~/.local/bin mediamtx

    pip install -e ".[dev]"
    anycam watch -c config.yaml

`config.yaml` needs the same `rtsp_port` and camera ids as the Windows side;
copy it across. The consumer ignores `source` and `encode` — those describe
capture, which happens on Windows.

The Windows host is discovered at runtime from the default route. Never
hardcode it: WSL's address changes across restarts.

## Reading the stats

    cam0     ok       frames=212      dropped=211      reconnects=0    age= 0.002s depth=1

| Field | Healthy | Meaning |
|---|---|---|
| `dropped` | climbing fast | Frames overwritten before you consumed them. **Correct** — it is staleness being discarded, not data loss you should fix. |
| `depth` | never above 1 | There is one slot. Latency cannot accumulate. |
| `age` | milliseconds | How old the frame you would act on is. |
| `reconnects` | stable | Growing means the stream keeps dropping. |
| `STALLED` | absent | No frame within `watchdog_timeout_s`; `error=` shows why. |

## Usage

```python
from anycam.client import MultiCameraClient
from anycam.config import load_config
from anycam.host import discover_windows_host

config = load_config("config.yaml")
with MultiCameraClient(config, discover_windows_host()) as client:
    while True:
        frame = client.take("cam0")      # None when nothing new has arrived
        if frame is not None:
            run_inference(frame.image)   # BGR ndarray, always the newest
```

### Or just use the RTSP URL

Each camera is a plain RTSP stream — VLC, ffmpeg, GStreamer and
`cv2.VideoCapture` all read it:

    rtsp://<windows-host>:8554/cam0

That is the right choice when your consumer keeps up with the frame rate. What
you give up is the reason this client exists: `VideoCapture` buffers internally,
so a consumer slower than the stream accumulates latency without bound — you end
up looking at a two-second-old scene while the picture still looks perfectly
fine. `CAP_PROP_BUFFERSIZE` is ignored by the FFMPEG backend. You also give up
runtime host discovery, reconnection with backoff, and the watchdog.

Prototype against the URL; switch to `MultiCameraClient` when inference falls
behind or the system needs to survive unattended.

## Testing

    pytest tests -m "not integration"   # 103 tests, no hardware needed
    pytest tests -m integration         # 5 tests, needs ffmpeg + mediamtx, no camera

The integration tier drives real ffmpeg, MediaMTX and PyAV against a synthetic
`testsrc2` source, with only DirectShow stubbed. It is worth keeping green: it
caught a reproducible segfault that 76 unit tests had approved, because closing
a PyAV container from another thread is a use-after-free and every fake's
`close()` was just setting an `Event`.

Hardware verification is documented in [docs/bench-checklist.md](docs/bench-checklist.md).

## Latency calibration

No in-band timestamp can measure glass-to-inference latency: a capture timestamp
is taken when a frame reaches software, already tens of milliseconds after light
hit the sensor. `tools/calibrate_latency.py` measures the whole chain — exposure,
USB transfer, encode, transport and decode — by filming a millisecond clock and
reading the digits off the saved frames against the arrival time.

**Run both halves in WSL.** A WSL terminal window is displayed by Windows, so
the camera can film it, and keeping the clock and the measurement on one clock
removes Windows/WSL skew from the result — WSL2 drifts from the host across
suspend/resume, exactly when a calibration run would mislead you.

    pip install -e ".[calibration]"
    python tools/calibrate_latency.py display                               # terminal 1
    python tools/calibrate_latency.py measure -c config.yaml --camera cam0  # terminal 2

Reading the digits is deliberately manual. Automating it with OCR would add a
dependency and a failure mode to a procedure that runs a handful of times per
hardware change.

## Verified

End to end against a Logitech C920 on Windows 11 with an RTX 5080: DirectShow
capture, NVENC encode, MediaMTX v1.9.3, RTSP over TCP, PyAV decode in WSL2 —
29 fps sustained, 2 ms frame age, `depth` never above 1, zero reconnects.

Component versions exercised: ffmpeg 7.0.2-static (WSL) and gyan.dev release-
essentials (Windows), MediaMTX v1.9.3, PyAV 18.1.0, Python 3.12 (WSL) and
3.14 (Windows).

Not verified: more than one camera at once, USB bandwidth limits at four
1080p30 streams, NVDEC decode in WSL, and real latency numbers. Those are bench
work — see the checklist.

## Design

See [the design spec](docs/superpowers/specs/2026-09-14-anycam-to-rtsp-design.md)
for why H.264 rather than MJPEG, why a wedged camera needs both a read timeout
and a watchdog, and why latency is measured out of band.
