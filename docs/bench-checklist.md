# Bench verification checklist

Everything here needs real hardware and is therefore verified by hand. The
automated suites cover the rest.

## 1. USB controller topology — do this first

Four 1080p30 MJPEG streams is roughly 24 MB/s against about 35 MB/s effective
on a single USB 2.0 controller. If all cameras share one root hub this works
until it doesn't, usually as intermittent drops on the last-enumerated camera.
No protocol choice fixes it.

- [ ] In Device Manager, View → Devices by connection; confirm which root hub
      each camera sits under
- [ ] Spread cameras across controllers where possible; prefer USB 3.0 ports
- [ ] Run all cameras for 10 minutes and confirm `anycam2rtsp watch` shows a stable
      frame rate on every one

## 2. DirectShow enumeration

- [ ] `anycam2rtsp devices` lists every camera
- [ ] Note that ffmpeg prints the list to **stderr**, and may exit zero or
      non-zero depending on the build (measured: ffmpeg 7.x exits 0, older
      builds exit 1). Neither is a failure
- [ ] Check for duplicate device names — two identical cameras report the same
      string, and the config cannot distinguish them by name alone
- [ ] Check for non-ASCII characters in device names

## 3. Hardware codecs

- [ ] Confirm `ffmpeg -decoders | findstr mjpeg_cuvid` lists the decoder; if
      absent, generate the config with `--no-hw-mjpeg`
- [ ] Confirm `ffmpeg -encoders | findstr h264_nvenc` lists the encoder
- [ ] In WSL, confirm NVDEC is used rather than software decode
- [ ] Watch GPU utilisation in `nvidia-smi` with all cameras running

## 4. Networking

- [ ] Confirm WSL reaches the host: `curl -v telnet://$(ip route show default \
      | awk '{print $3}'):8554`
- [ ] If refused, allow inbound TCP 8554 on the `vEthernet (WSL)` adapter
- [ ] Consider mirrored networking mode (`networkingMode=mirrored` in
      `.wslconfig`, supported on build 26200) — it removes this entire class
      of problem rather than documenting around it

## 5. Recovery

- [ ] Unplug a camera mid-stream; confirm the other cameras keep running
- [ ] Replug it; confirm the stream recovers without restarting anything
- [ ] Kill MediaMTX; confirm every client reconnects when it returns
- [ ] Simulate a wedged camera (frames stop arriving but the socket stays
      open) and confirm the stream recovers on its own within roughly 1-2
      seconds, racing the watchdog rather than reliably beating it (PyAV
      restarts its read timeout per read rather than running one
      continuous clock, so it only starts once any buffered RTP is
      consumed) — FFmpeg's read timeout aborts the blocked read, not the
      watchdog monitor
- [ ] Simulate a camera that keeps delivering frames but slower than
      `watchdog_timeout_s` apart (default 2s) and confirm the watchdog
      monitor forces a reconnect — this is the one wedge case the read
      timeout cannot see, because data never stops arriving

## 6. Latency calibration

- [ ] Run `tools/calibrate_latency.py` per the procedure in its docstring
      (needs the `calibration` extra: `pip install -e ".[calibration]"`)
- [ ] Run BOTH halves in WSL. The `display` clock shown in a WSL terminal is
      on the Windows screen for the camera to film, and sharing one clock
      between the display and the measurement removes Windows/WSL skew from
      the result
- [ ] Record the measured total latency per camera model
- [ ] Sanity check against the spec's budget: the camera itself is expected to
      dominate. A total far above roughly 60ms suggests the receiver's jitter
      buffer is not actually disabled.
