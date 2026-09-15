# anycam2rtsp — Windows capture host

This bundle is the **capture half** of anycam2rtsp. It runs on the Windows
machine the cameras are plugged into, and serves each camera as its own RTSP
stream. The consumer — `anycam2rtsp watch`, or your own inference code — runs in
WSL or on another Linux host and is not installed by this bundle.

```
USB cam ──DirectShow──▶ ffmpeg ──NVENC h264──▶ MediaMTX /camN
         (this machine, this bundle)                 │ RTSP over TCP
                                          WSL: PyAV ─┴─▶ your inference
```

## What you need

- Windows 10 or 11, 64-bit
- **Python 3.12 or newer** — <https://www.python.org/downloads/windows/>,
  ticking "Add python.exe to PATH". Only the config generator needs it; it
  pulls in one small dependency (PyYAML), not the heavy video stack.
- An NVIDIA GPU if you want hardware encoding. Without one it still works —
  see *No NVIDIA GPU* below.

Nothing is installed system-wide. Everything lands inside this folder.

## Quick start

Open PowerShell **in this folder** and run:

```powershell
.\run.ps1
```

That is the whole thing. It sets up on first run, finds your cameras, writes
`config.yaml` from their real names, works out which MJPEG decoder they
actually accept, generates the server config, and starts streaming.

If PowerShell refuses to run the scripts, allow them for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then check a camera from a browser on this machine:
**<http://localhost:8889/cam0>**. If the picture is there, capture and
encoding work, and anything failing on the WSL side is networking.

To consume it, in WSL:

```bash
anycam2rtsp watch -c config.yaml
```

`depth=1` with `dropped` climbing is *correct* — it means you are getting the
freshest frame rather than a backlog.

### Useful flags

| Command | What it does |
|---|---|
| `.\run.ps1 -Reconfigure` | Rebuild `config.yaml` from the cameras attached right now |
| `.\run.ps1 -Software` | Use `libx264` instead of NVENC (no NVIDIA GPU) |
| `.\run.ps1 -NoProbe` | Trust `config.yaml` as written; skip the decoder check |

## What you need

- Windows 10 or 11, 64-bit
- **Python 3.12 or newer** — <https://www.python.org/downloads/windows/>,
  ticking "Add python.exe to PATH". Only the config generator needs it; it
  pulls in one small dependency (PyYAML), not the heavy video stack.
- An NVIDIA GPU for hardware encoding. Without one, use `-Software`.

Nothing is installed system-wide. Everything lands inside this folder.

## The individual steps

`run.ps1` just calls these in order. Run them yourself when you want control:

| Script | Does |
|---|---|
| `.\run.ps1` | All of the below, in order, skipping what is already done |
| `.\setup.ps1` | venv, package, ffmpeg + MediaMTX into `bin\` |
| `.\devices.ps1` | List DirectShow cameras (`-Raw` for ffmpeg's full output) |
| `.\configure.ps1` | Write `config.yaml` from detected cameras (`-Choose` to pick) |
| `.\generate.ps1` | Build `mediamtx.yml` (`-NoHwMjpeg` for CPU MJPEG decode) |
| `.\start.ps1` | Run the server |
| `.\firewall.ps1` | Allow inbound TCP 8554 (needs administrator) |

`lib\` holds internals the scripts share, not commands to run: `common.ps1`
(native-command helpers) and `list_cameras.py` (reads `config.yaml` via the
bundled package). `src\` is that package; `bin\` is where setup puts ffmpeg
and MediaMTX.

Edit `config.yaml` by hand whenever you like — change resolution, frame rate,
bitrate, or camera ids. Re-run `.\generate.ps1` afterwards. **Do not hand-edit
`mediamtx.yml`**: the ffmpeg command lines in it are escaped for MediaMTX's own
argument parser, and getting that wrong produces a camera that never starts and
logs nothing.

### Firewall

Often not needed — test first. Under WSL2's default NAT networking, inbound
connections on the `vEthernet (WSL)` adapter may be blocked, giving
"connection refused" from WSL with nothing explaining why. If so, run
`.\firewall.ps1` from an **administrator** PowerShell.

Better, if you can: put

```ini
[wsl2]
networkingMode=mirrored
```

in `%USERPROFILE%\.wslconfig` and run `wsl --shutdown`. WSL then reaches the
host on `127.0.0.1` and no rule is needed. Requires Windows 11 22H2 or newer.

## When it doesn't work

| Symptom | What it means |
|---|---|
| `http://localhost:8889/cam0` is blank on Windows | Capture problem, not networking. Check MediaMTX's log for that path's ffmpeg — usually a device-name mismatch. |
| Browser works, WSL gets connection refused | Firewall. Run `.\firewall.ps1` as administrator, or switch to mirrored networking. |
| One camera dead, the others fine | Working as designed — cameras are fully independent. Check that one's device name. |
| All cameras stutter, the last one worst | USB bandwidth. Four 1080p30 MJPEG streams is ~24 MB/s against ~35 MB/s on one USB 2.0 controller. In Device Manager use *View → Devices by connection* and spread them across root hubs. |
| MediaMTX exits immediately on start | It aborts on unknown config keys. Make sure `bin\mediamtx.exe` is v1.9.3 — a newer release renamed keys this generator emits. |
| `CUDA_ERROR_NO_DEVICE`, `cuvid decode callback error` | The GPU MJPEG decoder cannot open your camera. Run `.\generate.ps1 -NoHwMjpeg` and restart. This happens even on machines with a working NVIDIA GPU and a working `h264_nvenc` encoder — see below. |

## No NVIDIA GPU

Change the encoder in `config.yaml` and regenerate:

```yaml
    encode: {codec: libx264, preset: ultrafast, tune: zerolatency, bitrate: 8M, gop: 30}
```

The NVENC-only flags are omitted automatically for non-NVENC codecs. Expect
higher CPU use and somewhat more latency; four 1080p30 streams on software
x264 is demanding.

## Measuring real latency

No timestamp inside the stream can tell you glass-to-inference latency — a
capture timestamp is taken when the frame reaches software, already tens of
milliseconds after light hit the sensor. Point a camera at a millisecond clock
on screen and compare; `tools/calibrate_latency.py` in the main repo automates
the capture half. `docs/bench-checklist.md` has the full procedure.

## When `mjpeg_cuvid` is listed but does not work

`setup.ps1` reports whether your ffmpeg build *lists* `mjpeg_cuvid`. That is
not the same as it working. On at least one RTX 5080 machine it decodes MJPEG
*files* correctly and still fails against a live DirectShow camera with:

    CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected
    cuvid decode callback error

on that same machine `h264_nvenc` encodes fine and `nvidia-smi` is healthy, so
this is specific to the CUVID decoder opening a capture device, not a broken
GPU or driver.

The fix is one flag:

```powershell
.\generate.ps1 -NoHwMjpeg
.\start.ps1
```

What you give up is small. The camera already delivers MJPEG, so the choice is
only *where* that MJPEG is decoded before being encoded to H.264. On the GPU
the frame never leaves VRAM; on the CPU it costs roughly 5 ms per 1080p frame
per camera — negligible for one or two cameras, around half a core for four.
**Encoding stays on the GPU either way**, which is the expensive half.
