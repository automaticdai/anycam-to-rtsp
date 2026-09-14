# anycam-to-rtsp — Design

**Date:** 2026-09-14
**Status:** Approved design, pending implementation plan

## Purpose

Stream 2–4 USB cameras attached to a Windows host into WSL2 for real-time
computer-vision inference, keeping camera drivers on Windows and delivering
the freshest available frame rather than a complete frame sequence.

## Requirements

| Requirement | Value |
|---|---|
| Cameras | 2–4, independent (no cross-camera time alignment) |
| Per camera | 1920x1080 @ 30fps |
| Consumer | Real-time CV inference in WSL2 Python |
| Latency | Minimise; staleness is worse than loss |
| Control | One-way stream now; control channel reserved in the architecture |
| Capture backend | Windows DirectShow only; source swappable by config |

Dropping frames is correct behaviour when inference falls behind. The system
must degrade to a lower effective frame rate, never to growing latency.

## Verified environment

Confirmed on the target machine, not assumed:

- Windows 11 Enterprise, build 10.0.26200
- NVIDIA GeForce RTX 5080 (NVENC + NVDEC), driver 616.92
- AMD Radeon integrated graphics (secondary encoder, unused by this design)
- WSL2 kernel 6.6.87.2, GPU passthrough live: `/dev/dxg` present, `nvidia-smi`
  functional inside WSL
- WSL2 networking: **NAT mode** (gateway `172.30.64.1`, DNS proxy
  `10.255.255.254`). Not mirrored.
- No `ffmpeg` on either side. No `cv2`, `av`, or `zmq` in WSL. Python 3.12.3.

Encode and decode therefore run on the *same physical GPU*, reached from both
sides of the WSL boundary.

## Architecture

```
USB cam ──DirectShow──▶ ffmpeg ──NVENC h264──▶ MediaMTX /camN
                                                    │ RTSP over TCP
                          WSL: PyAV ──NVDEC──▶ deque(maxlen=1) ──▶ inference
```

One ffmpeg process, one RTSP path, one receiver thread, and one watchdog per
camera. No shared state between cameras.

### Windows listens, WSL connects

Forced by NAT mode: the WSL IP changes across restarts, so anything pushing
*to* WSL is fragile. WSL discovers the host at runtime from
`ip route show default` and connects outward. Reconnection then reduces to a
loop around `connect()`, satisfying the reconnect requirement structurally.

### MediaMTX as the RTSP server

Chosen over bare `ffmpeg -rtsp_flags listen`, which permits only one client per
stream and offers no reconnect handling or introspection. MediaMTX additionally
exposes WebRTC, restoring the browser-inspectable property that made MJPEG
attractive — a camera can be eyeballed without running the Python client.

### GPU-resident capture path

Cameras negotiate MJPEG at 1080p30 (USB 2.0 cannot carry raw 1080p30), so the
Windows side must transcode MJPEG → H.264. Decoding via `mjpeg_cuvid` with
`-hwaccel_output_format cuda` keeps the frame in VRAM from decode through
encode, with no host copies:

```
ffmpeg -hide_banner -loglevel warning \
  -f dshow -rtbufsize 64M -framerate 30 -video_size 1920x1080 \
  -c:v mjpeg_cuvid -hwaccel_output_format cuda \
  -i video="<device name>" \
  -c:v h264_nvenc -preset p1 -tune ull -rc cbr -b:v 8M -bf 0 -g 30 -delay 0 \
  -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/cam0
```

`mjpeg_cuvid` availability is build-dependent; the design falls back to CPU
MJPEG decode when the decoder is absent. Cameras exposing native UVC H.264 can
skip the transcode entirely with `-c:v copy`, but this is not assumed.

### Receiver configuration

The RTSP demuxer's jitter buffer is the one stage that can silently add more
delay than the entire rest of the pipeline, and it is invisible without
measurement. It must be disabled explicitly:

```python
options = {
    "rtsp_transport":     "tcp",
    "fflags":             "nobuffer",
    "flags":              "low_delay",
    "probesize":          "32",
    "analyzeduration":    "0",
    "reorder_queue_size": "0",
    "max_delay":          "0",
}
```

PyAV is required over `cv2.VideoCapture`, which buffers internally and exposes
neither PTS nor low-delay flags. NVDEC is used when PyAV exposes CUDA hwaccel,
falling back to software decode otherwise.

RTSP runs over TCP rather than UDP: on a virtual NIC there is no packet loss to
route around, so UDP would contribute corruption modes and no benefit.

GOP is 30 rather than all-intra. All-intra would restore drop-before-decode,
but hardware decode is cheap enough that paying 4–6x bitrate for the property
is not justified.

## Frame identity and timing

H.264 has no per-frame metadata slot. `h264_metadata`'s `sei_user_data` injects
a *fixed* SEI payload set at process launch — adequate for a camera ID, useless
for timestamps. Genuine per-frame SEI requires owning the NAL stream between
encoder and muxer, abandoning ffmpeg CLI for direct NVENC bindings; rejected as
disproportionate.

`-use_wallclock_as_timestamps 1` is explicitly **not** used. It stamps packet
arrival time in WSL, which already contains the latency being measured, and
would produce a confident number structurally incapable of being correct.

Timing is therefore split across three layers:

**Identity (free).** Camera ID is the RTSP path. Frame ID is a per-stream
receive counter. PTS is read from the packet via PyAV.

**Sender clock (estimate, labelled as such).** RTCP sender reports map RTP
timestamps to sender wall clock, and FFmpeg's RTSP demuxer processes them. An
NTP-style offset handshake on the reserved control channel corrects
Windows↔WSL clock skew — WSL2 syncs to the host but drifts across
suspend/resume, precisely when it would mislead. This path is finicky in
practice and is treated as an estimate with a known error bar.

**True latency (measured out of band).** No in-band timestamp can capture
glass-to-inference latency, because a capture timestamp is taken when a frame
reaches software, already tens of milliseconds after exposure. A calibration
harness displays a millisecond timer on the Windows screen, points a camera at
it, and compares decoded digits against the WSL clock. Run once per hardware
configuration; yields a constant added to the estimate.

### Expected latency budget

| Stage | Cost |
|---|---|
| Sensor exposure + USB transfer | 10–30ms |
| DirectShow → ffmpeg buffer | 2–5ms |
| NVENC (`-tune ull -preset p1 -bf 0`) | 3–5ms |
| MediaMTX relay + vNIC | 1–2ms |
| RTSP demux jitter buffer | 0–40ms (must be tuned to ~0) |
| NVDEC decode | 2–3ms |
| Queue wait | ~0 by construction |

The camera itself is expected to dominate. This is the sanity anchor for
calibration results.

## Freshness and failure

**Freshness is structural.** Each camera's receiver thread owns a
`deque(maxlen=1)`. The decode loop never blocks on the consumer; inference
always takes the newest frame and older frames are overwritten. There is no
staleness threshold to tune and no mechanism by which a backlog can accumulate.

> **Correction (post-Task 10).** An earlier version of this section described
> layer 3 as a watchdog that forces a reconnect by closing the receiver's
> container from another thread. Task 10's integration suite found that
> mechanism unsafe against real PyAV — a libav container may only be closed
> by the thread that owns it, and closing it while that thread is inside
> `decode()` is a use-after-free that segfaulted the process. It was replaced
> before this system was built; the description below is what actually ships.
> Recorded here so the closing-from-another-thread design is not re-derived.

**Three independent supervision layers**, because the failure modes are
independent:

1. MediaMTX restarts a dead ffmpeg child (camera unplugged, driver fault)
2. The WSL client reconnects its RTSP session with exponential backoff:
   200ms initial, doubling, capped at 5s, reset to initial on a successful
   frame (server restart, path not yet published)
3. A per-stream watchdog monitor thread catches the one case the mechanisms
   below cannot: a stream that keeps delivering frames, just too slowly to
   beat `watchdog_timeout_s` (2.0s) between them. It polls each receiver's
   watchdog from outside the receiver thread and calls `force_reconnect()`,
   which sets a flag the decode loop checks once per decoded frame.

A container is only ever opened, read from, and closed by its own receiver
thread — never by the watchdog monitor or any other thread. Blocking I/O
inside that thread is instead bounded by FFmpeg's own interrupt callback,
wired through PyAV as `av.open(url, options=..., timeout=(OPEN_TIMEOUT_S,
READ_TIMEOUT_S))` (currently 3.0s / 1.0s). A stream that stops delivering
data entirely — the classic "wedged camera holds a healthy TCP connection"
case — therefore aborts its own blocked read and reconnects by itself, in
under `READ_TIMEOUT_S`, without any external intervention. This self-heal
races the watchdog rather than reliably beating it: PyAV restarts its read
timeout on every `av_read_frame` call rather than running one continuous
clock since the last frame, so the clock only starts once any already-
buffered RTP is consumed. Measured against real PyAV, a full no-data wedge
resolves in ~1-2s (2.01s at realtime pacing, 2.13-2.23s at burst pacing) at
the 2s default `watchdog_timeout_s` — not the ~1s a naive reading of
`READ_TIMEOUT_S` alone would suggest. Both mechanisms converge on a
reconnect either way, so this is a documentation precision issue, not a
correctness one.

The watchdog monitor's remaining unique role is therefore narrower than a
"forces every reconnect" description would suggest: it exists only for the
case where the read timeout structurally cannot fire — frames keep arriving,
just too slowly — because `force_reconnect()`'s flag is only observed between
decoded frames and cannot unblock a read that has already stopped producing
anything to check the flag against.

Camera failures are isolated: separate process, path, thread, and watchdog.

## Observability

`--stats` is a feature, not a test aid. This system's failure modes are silent —
a frozen camera keeps its connection, a misconfigured jitter buffer adds delay
nothing reports, a saturated USB controller degrades one camera while the others
look perfect. Per camera, continuously: fps, drop rate, queue age, reconnect
count, decode time, time since last frame.

## Testing

**No hardware required** (written test-first):

- Freshness queue: producer outruns consumer → newest frame always delivered,
  depth never exceeds 1, drop count matches overwrites
- Reconnect backoff: fake transport failing on schedule; assert sequence,
  ceiling, reset-on-success
- Watchdog: injectable clock, never `sleep`; assert failure at threshold
- Clock-offset estimator: synthetic round-trip samples → offset and error bound
- Per-camera isolation: kill one supervisor, assert the other three unaffected

**Synthetic camera, no USB hardware** — the high-leverage tier:

```
ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=30 ... → MediaMTX → PyAV client
```

`testsrc2` burns a frame counter into the image, so the client decodes it and
asserts objectively which frames arrived and which were dropped — ground truth
independent of the system's own timestamping. Exercises the real transport end
to end with only DirectShow stubbed. Runs in CI on plain Linux using `libx264`
and software decode, validating protocol and supervision logic without a GPU.

**Bench verification** (written checklist, not automated): DirectShow
enumeration quirks (duplicate device names, non-ASCII names, and the misleading
exit code from `-list_devices`, which varies by build), NVENC/NVDEC on real
hardware, the
four-camera USB bandwidth ceiling, unplug/replug recovery, latency calibration.

## Configuration

```yaml
server:
  rtsp_port: 8554
cameras:
  - id: cam0
    source:
      type: dshow            # dshow | lavfi (test)
      device: "Logitech BRIO"
    video:  {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
client:
  watchdog_timeout_s: 2.0
  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}
```

The `source.type` discriminator is the swappable-backend seam. It exists to
serve the `testsrc2` harness; other capture backends are out of scope but not
precluded.

## Out of scope

- Capture backends other than DirectShow (v4l2, avfoundation)
- ONVIF/IP cameras as first-class inputs
- Cross-camera time alignment or stereo synchronisation
- Runtime control (start/stop, exposure, focus) — channel reserved only
- Recording to disk

## Known risks

1. **USB bandwidth.** Four 1080p30 MJPEG streams is ~24 MB/s against ~35 MB/s
   effective on a single USB 2.0 controller. If all four cameras share a root
   hub this works until it doesn't, typically as intermittent drops on the
   last-enumerated camera. A hardware finding no protocol choice can fix;
   confirm controller topology early.
2. **Windows Firewall.** Inbound connections on the `vEthernet (WSL)` adapter
   are blocked by default, producing "connection refused" with no clear cause.
   Mirrored networking mode (supported on build 26200) would remove this class
   of problem entirely and is worth evaluating before writing socket code.
3. **`mjpeg_cuvid` availability** is ffmpeg-build-dependent; CPU fallback
   specified above.
4. **NVDEC exposure through PyAV** varies by version; software decode fallback
   specified above.
5. **RTCP SR wall-clock recovery** through PyAV is unreliable in practice,
   which is why the calibration harness exists as the authoritative measurement.
