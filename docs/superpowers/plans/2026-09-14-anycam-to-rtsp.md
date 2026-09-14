# anycam-to-rtsp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream 2–4 Windows USB cameras at 1080p30 into WSL2 for real-time inference, delivering the freshest available frame per camera and never accumulating latency.

**Architecture:** Windows runs one ffmpeg process per camera (DirectShow capture → NVENC H.264), supervised by MediaMTX which serves each camera as its own RTSP path. WSL connects outward as a client, decodes with PyAV/NVDEC, and keeps exactly one frame per camera in a `maxlen=1` buffer. Per-camera watchdog and reconnect run independently so one dead camera cannot affect the others.

**Tech Stack:** Python 3.12, PyAV, PyYAML, NumPy, pytest; ffmpeg + MediaMTX as external binaries.

**Spec:** `docs/superpowers/specs/2026-09-14-anycam-to-rtsp-design.md`

## Global Constraints

- Python **3.12** (verified present: 3.12.3).
- Cameras: **2–4**, each **1920x1080 @ 30fps**, treated as fully independent — no cross-camera alignment anywhere in the code.
- RTSP transport is **TCP**, never UDP.
- Watchdog timeout: **2.0s**. Backoff: **initial 0.2s, factor 2.0, max 5.0s**, reset on a successful frame.
- GOP: **30**. B-frames: **0**.
- Receiver options are mandatory and must never be omitted: `rtsp_transport=tcp`, `fflags=nobuffer`, `flags=low_delay`, `probesize=32`, `analyzeduration=0`, `reorder_queue_size=0`, `max_delay=0`.
- **Never** use `-use_wallclock_as_timestamps`. It stamps arrival time in WSL, not capture time.
- WSL networking is **NAT mode**; the Windows host is discovered at runtime from `ip route show default`. Never hardcode an IP.
- Every clock-dependent unit takes an **injectable clock**. No `time.sleep` in unit tests.
- **Deferred from the spec, deliberately:** the NTP-style clock-offset
  handshake depends on the control channel, which the spec reserves but does
  not build. `est_capture_ns` therefore carries the RTCP-derived estimate
  alone, and the calibration harness (Task 11) remains the authoritative
  latency measurement. Do not invent a control channel to close this gap.

---

### Task 1: Project scaffolding, Frame, and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/anycam/__init__.py`
- Create: `src/anycam/frame.py`
- Create: `src/anycam/config.py`
- Create: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Frame`, `SourceConfig`, `VideoConfig`, `EncodeConfig`, `BackoffConfig`, `ClientConfig`, `ServerConfig`, `CameraConfig`, `AppConfig`, `load_config(path: str | Path) -> AppConfig`, `ConfigError`.

- [ ] **Step 1: Create the project skeleton**

```bash
mkdir -p src/anycam tests/integration tools docs
touch src/anycam/__init__.py tests/__init__.py
```

`pyproject.toml`:

```toml
[project]
name = "anycam-to-rtsp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["av>=12.0", "PyYAML>=6.0", "numpy>=1.26"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
anycam = "anycam.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: requires ffmpeg and mediamtx binaries"]
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

- [ ] **Step 2: Write the failing config tests**

`tests/test_config.py`:

```python
import pytest
from anycam.config import load_config, ConfigError


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


VALID = """
server:
  rtsp_port: 8554
cameras:
  - id: cam0
    source: {type: dshow, device: "Logitech BRIO"}
    video: {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
client:
  watchdog_timeout_s: 2.0
  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}
"""


def test_loads_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.server.rtsp_port == 8554
    assert len(cfg.cameras) == 1
    assert cfg.cameras[0].id == "cam0"
    assert cfg.cameras[0].source.device == "Logitech BRIO"
    assert cfg.cameras[0].video.fps == 30
    assert cfg.client.watchdog_timeout_s == 2.0
    assert cfg.client.backoff.max_s == 5.0


def test_client_section_defaults_to_spec_values(tmp_path):
    text = VALID.replace(
        "client:\n  watchdog_timeout_s: 2.0\n"
        "  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}\n", "")
    cfg = load_config(write(tmp_path, text))
    assert cfg.client.watchdog_timeout_s == 2.0
    assert cfg.client.backoff.initial_s == 0.2
    assert cfg.client.backoff.factor == 2.0
    assert cfg.client.backoff.max_s == 5.0


def test_rejects_unknown_source_type(tmp_path):
    text = VALID.replace("type: dshow", "type: carrier_pigeon")
    with pytest.raises(ConfigError, match="carrier_pigeon"):
        load_config(write(tmp_path, text))


def test_rejects_dshow_without_device(tmp_path):
    text = VALID.replace('source: {type: dshow, device: "Logitech BRIO"}',
                         "source: {type: dshow}")
    with pytest.raises(ConfigError, match="device"):
        load_config(write(tmp_path, text))


def test_rejects_duplicate_camera_ids(tmp_path):
    text = """
server:
  rtsp_port: 8554
cameras:
  - id: cam0
    source: {type: lavfi}
    video: {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
  - id: cam0
    source: {type: lavfi}
    video: {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
"""
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_rejects_zero_cameras(tmp_path):
    with pytest.raises(ConfigError, match="at least one camera"):
        load_config(write(tmp_path, "server: {rtsp_port: 8554}\ncameras: []\n"))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.config'`

- [ ] **Step 4: Implement `Frame`**

`src/anycam/frame.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Frame:
    """One decoded frame plus the identity and timing we can obtain for free.

    `est_capture_ns` is an estimate derived from the sender clock and is
    documented as such in the spec; it is never treated as ground truth.
    """

    camera_id: str
    frame_id: int
    pts: int | None
    recv_ns: int
    decoded_ns: int
    image: Any  # numpy.ndarray, typed loosely to avoid importing numpy here
    est_capture_ns: int | None = None
```

- [ ] **Step 5: Implement `config.py`**

`src/anycam/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_SOURCE_TYPES = {"dshow", "lavfi"}


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    type: str
    device: str | None = None


@dataclass(frozen=True, slots=True)
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30


@dataclass(frozen=True, slots=True)
class EncodeConfig:
    codec: str = "h264_nvenc"
    preset: str = "p1"
    tune: str = "ull"
    bitrate: str = "8M"
    gop: int = 30


@dataclass(frozen=True, slots=True)
class BackoffConfig:
    initial_s: float = 0.2
    factor: float = 2.0
    max_s: float = 5.0


@dataclass(frozen=True, slots=True)
class ClientConfig:
    watchdog_timeout_s: float = 2.0
    backoff: BackoffConfig = field(default_factory=BackoffConfig)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    rtsp_port: int = 8554


@dataclass(frozen=True, slots=True)
class CameraConfig:
    id: str
    source: SourceConfig
    video: VideoConfig
    encode: EncodeConfig


@dataclass(frozen=True, slots=True)
class AppConfig:
    server: ServerConfig
    cameras: tuple[CameraConfig, ...]
    client: ClientConfig


def _camera(raw: dict) -> CameraConfig:
    if "id" not in raw:
        raise ConfigError("camera entry is missing 'id'")
    src = raw.get("source") or {}
    stype = src.get("type")
    if stype not in VALID_SOURCE_TYPES:
        raise ConfigError(
            f"camera {raw['id']}: unknown source type {stype!r}; "
            f"expected one of {sorted(VALID_SOURCE_TYPES)}")
    if stype == "dshow" and not src.get("device"):
        raise ConfigError(f"camera {raw['id']}: dshow source requires 'device'")
    return CameraConfig(
        id=str(raw["id"]),
        source=SourceConfig(type=stype, device=src.get("device")),
        video=VideoConfig(**(raw.get("video") or {})),
        encode=EncodeConfig(**(raw.get("encode") or {})),
    )


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cams_raw = raw.get("cameras") or []
    if not cams_raw:
        raise ConfigError("configuration must define at least one camera")

    cameras = tuple(_camera(c) for c in cams_raw)
    ids = [c.id for c in cameras]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate camera ids: {sorted(dupes)}")

    client_raw = dict(raw.get("client") or {})
    backoff = BackoffConfig(**(client_raw.pop("backoff", None) or {}))
    return AppConfig(
        server=ServerConfig(**(raw.get("server") or {})),
        cameras=cameras,
        client=ClientConfig(backoff=backoff, **client_raw),
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Write `config.example.yaml`**

```yaml
server:
  rtsp_port: 8554

cameras:
  - id: cam0
    source: {type: dshow, device: "Logitech BRIO"}
    video:  {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
  - id: cam1
    source: {type: dshow, device: "HD Pro Webcam C920"}
    video:  {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}

client:
  watchdog_timeout_s: 2.0
  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml config.example.yaml src/anycam tests
git commit -m "feat: project scaffolding, Frame, and config loading"
```

---

### Task 2: Windows host discovery

**Files:**
- Create: `src/anycam/host.py`
- Test: `tests/test_host.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_default_gateway(route_output: str) -> str`, `discover_windows_host() -> str`, `HostDiscoveryError`.

WSL is in NAT mode, so the Windows host is the default gateway. The parse is a
pure function so it can be tested without touching the live system.

- [ ] **Step 1: Write the failing test**

`tests/test_host.py`:

```python
import pytest
from anycam.host import parse_default_gateway, HostDiscoveryError

REAL = "default via 172.30.64.1 dev eth0 proto kernel \n"


def test_parses_gateway_from_real_wsl_output():
    assert parse_default_gateway(REAL) == "172.30.64.1"


def test_parses_when_multiple_routes_present():
    out = ("default via 172.30.64.1 dev eth0 proto kernel \n"
           "172.30.64.0/20 dev eth0 proto kernel scope link src 172.30.70.5\n")
    assert parse_default_gateway(out) == "172.30.64.1"


def test_raises_when_no_default_route():
    with pytest.raises(HostDiscoveryError, match="no default route"):
        parse_default_gateway("172.30.64.0/20 dev eth0 scope link\n")


def test_raises_on_empty_output():
    with pytest.raises(HostDiscoveryError):
        parse_default_gateway("")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_host.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.host'`

- [ ] **Step 3: Implement**

`src/anycam/host.py`:

```python
from __future__ import annotations

import subprocess


class HostDiscoveryError(RuntimeError):
    """Raised when the Windows host address cannot be determined."""


def parse_default_gateway(route_output: str) -> str:
    """Extract the default gateway address from `ip route show default`.

    Under WSL2 NAT networking the default gateway is the Windows host. The
    address changes across restarts, so it must never be hardcoded.
    """
    for line in route_output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "default" and fields[1] == "via":
            return fields[2]
    raise HostDiscoveryError(
        f"no default route found in: {route_output!r}")


def discover_windows_host() -> str:
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, check=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostDiscoveryError(f"could not run 'ip route': {exc}") from exc
    return parse_default_gateway(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_host.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/anycam/host.py tests/test_host.py
git commit -m "feat: discover Windows host from WSL default route"
```

---

### Task 3: Freshness buffer

**Files:**
- Create: `src/anycam/freshness.py`
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `Frame` (Task 1).
- Produces: `LatestFrameBuffer` with `.put(frame) -> None`, `.get() -> Frame | None`, `.take() -> Frame | None`, properties `.dropped: int`, `.accepted: int`, `.depth: int`.

This is the component the whole architecture exists to provide. `get()` returns
the newest frame without consuming it; `take()` returns it and clears the slot.

- [ ] **Step 1: Write the failing test**

`tests/test_freshness.py`:

```python
import threading

from anycam.frame import Frame
from anycam.freshness import LatestFrameBuffer


def frame(n):
    return Frame(camera_id="cam0", frame_id=n, pts=n * 3000,
                 recv_ns=n, decoded_ns=n, image=None)


def test_get_returns_newest_frame():
    buf = LatestFrameBuffer()
    for i in range(5):
        buf.put(frame(i))
    assert buf.get().frame_id == 4


def test_depth_never_exceeds_one():
    buf = LatestFrameBuffer()
    for i in range(100):
        buf.put(frame(i))
        assert buf.depth <= 1


def test_drop_count_matches_overwrites():
    buf = LatestFrameBuffer()
    for i in range(10):
        buf.put(frame(i))
    # 10 put, 9 overwritten, 1 retained
    assert buf.accepted == 10
    assert buf.dropped == 9


def test_take_consumes_the_frame():
    buf = LatestFrameBuffer()
    buf.put(frame(1))
    assert buf.take().frame_id == 1
    assert buf.take() is None
    assert buf.get() is None


def test_consumed_frame_is_not_counted_as_dropped():
    buf = LatestFrameBuffer()
    buf.put(frame(1))
    buf.take()
    buf.put(frame(2))
    assert buf.dropped == 0


def test_empty_buffer_returns_none():
    assert LatestFrameBuffer().get() is None


def test_producer_outrunning_consumer_always_yields_newest():
    buf = LatestFrameBuffer()
    seen = []
    stop = threading.Event()

    def produce():
        for i in range(2000):
            buf.put(frame(i))
        stop.set()

    t = threading.Thread(target=produce)
    t.start()
    while not stop.is_set():
        got = buf.take()
        if got is not None:
            seen.append(got.frame_id)
    t.join()

    assert seen == sorted(seen), "frames must never be delivered out of order"
    assert buf.accepted == 2000
    assert buf.accepted - buf.dropped == len(seen) + buf.depth
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.freshness'`

- [ ] **Step 3: Implement**

`src/anycam/freshness.py`:

```python
from __future__ import annotations

import threading

from .frame import Frame


class LatestFrameBuffer:
    """Holds at most one frame: the newest one produced.

    Freshness is structural rather than threshold-driven. A producer never
    blocks, and a slow consumer degrades to a lower effective frame rate
    instead of to growing latency, because there is no queue in which a
    backlog could accumulate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._accepted = 0
        self._dropped = 0

    def put(self, frame: Frame) -> None:
        with self._lock:
            if self._frame is not None:
                self._dropped += 1
            self._frame = frame
            self._accepted += 1

    def get(self) -> Frame | None:
        """Newest frame, left in place."""
        with self._lock:
            return self._frame

    def take(self) -> Frame | None:
        """Newest frame, clearing the slot so it is not counted as dropped."""
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    @property
    def depth(self) -> int:
        with self._lock:
            return 0 if self._frame is None else 1

    @property
    def accepted(self) -> int:
        with self._lock:
            return self._accepted

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_freshness.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/anycam/freshness.py tests/test_freshness.py
git commit -m "feat: newest-frame-wins buffer with drop accounting"
```

---

### Task 4: Backoff and watchdog

**Files:**
- Create: `src/anycam/backoff.py`
- Create: `src/anycam/watchdog.py`
- Test: `tests/test_backoff.py`
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Consumes: `BackoffConfig` (Task 1).
- Produces: `Backoff(config)` with `.next_delay() -> float`, `.reset() -> None`, `.attempts: int`; `Watchdog(timeout_s, clock)` with `.beat() -> None`, `.expired() -> bool`, `.age_s() -> float`.

Both take an injectable clock. Tests must never call `time.sleep`; a test that
sleeps is slow and flaky and tells you less than a fake clock does.

- [ ] **Step 1: Write the failing backoff test**

`tests/test_backoff.py`:

```python
from anycam.backoff import Backoff
from anycam.config import BackoffConfig

CFG = BackoffConfig(initial_s=0.2, factor=2.0, max_s=5.0)


def test_first_delay_is_initial():
    assert Backoff(CFG).next_delay() == 0.2


def test_delays_double_up_to_the_cap():
    b = Backoff(CFG)
    assert [b.next_delay() for _ in range(8)] == [
        0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0]


def test_reset_returns_to_initial():
    b = Backoff(CFG)
    for _ in range(5):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 0.2


def test_attempts_counts_delays_since_reset():
    b = Backoff(CFG)
    b.next_delay()
    b.next_delay()
    assert b.attempts == 2
    b.reset()
    assert b.attempts == 0
```

- [ ] **Step 2: Write the failing watchdog test**

`tests/test_watchdog.py`:

```python
from anycam.watchdog import Watchdog


class FakeClock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1e9)


def test_fresh_watchdog_is_not_expired():
    assert not Watchdog(2.0, clock=FakeClock()).expired()


def test_expires_after_timeout_without_a_beat():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(1.9)
    assert not wd.expired()
    clock.advance(0.2)
    assert wd.expired()


def test_beat_resets_the_timer():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(1.9)
    wd.beat()
    clock.advance(1.9)
    assert not wd.expired()


def test_age_reports_time_since_last_beat():
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    clock.advance(0.75)
    assert abs(wd.age_s() - 0.75) < 1e-6


def test_a_wedged_stream_expires_even_though_nothing_raised():
    """The failure mode this exists for: the socket is fine, frames stopped."""
    clock = FakeClock()
    wd = Watchdog(2.0, clock=clock)
    for _ in range(30):          # one second of healthy 30fps
        clock.advance(1 / 30)
        wd.beat()
    assert not wd.expired()
    clock.advance(2.5)           # camera wedges, connection stays open
    assert wd.expired()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_backoff.py tests/test_watchdog.py -v`
Expected: FAIL — `ModuleNotFoundError` for `anycam.backoff` and `anycam.watchdog`

- [ ] **Step 4: Implement backoff**

`src/anycam/backoff.py`:

```python
from __future__ import annotations

from .config import BackoffConfig


class Backoff:
    """Exponential backoff with a ceiling, reset on success."""

    def __init__(self, config: BackoffConfig) -> None:
        self._config = config
        self._attempts = 0

    def next_delay(self) -> float:
        delay = self._config.initial_s * (self._config.factor ** self._attempts)
        self._attempts += 1
        return min(delay, self._config.max_s)

    def reset(self) -> None:
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts
```

- [ ] **Step 5: Implement watchdog**

`src/anycam/watchdog.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable


class Watchdog:
    """Declares failure when frames stop arriving, regardless of socket state.

    This is not redundant with reconnect logic. A wedged USB camera holds a
    healthy TCP connection indefinitely, so nothing raises and nothing closes;
    the only observable symptom is that frames stopped.
    """

    def __init__(self, timeout_s: float,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._timeout_ns = int(timeout_s * 1e9)
        self._clock = clock
        self._last_ns = clock()

    def beat(self) -> None:
        self._last_ns = self._clock()

    def expired(self) -> bool:
        return (self._clock() - self._last_ns) > self._timeout_ns

    def age_s(self) -> float:
        return (self._clock() - self._last_ns) / 1e9
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backoff.py tests/test_watchdog.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Commit**

```bash
git add src/anycam/backoff.py src/anycam/watchdog.py tests/test_backoff.py tests/test_watchdog.py
git commit -m "feat: reconnect backoff and stream watchdog"
```

---

### Task 5: ffmpeg capture command builder

**Files:**
- Create: `src/anycam/ffmpeg_cmd.py`
- Test: `tests/test_ffmpeg_cmd.py`

**Interfaces:**
- Consumes: `CameraConfig` (Task 1).
- Produces: `build_capture_command(camera, rtsp_url, *, hw_mjpeg_decode=True) -> list[str]`, `command_string(argv) -> str`.

A pure function, so every encoder flag decision in the spec becomes a unit test
rather than something you discover on the bench. `hw_mjpeg_decode=False` is the
documented fallback for ffmpeg builds lacking `mjpeg_cuvid`.

- [ ] **Step 1: Write the failing test**

`tests/test_ffmpeg_cmd.py`:

```python
import pytest

from anycam.config import CameraConfig, EncodeConfig, SourceConfig, VideoConfig
from anycam.ffmpeg_cmd import build_capture_command, command_string


def dshow_cam(**encode):
    return CameraConfig(
        id="cam0",
        source=SourceConfig(type="dshow", device="Logitech BRIO"),
        video=VideoConfig(1920, 1080, 30),
        encode=EncodeConfig(**encode) if encode else EncodeConfig(),
    )


URL = "rtsp://127.0.0.1:8554/cam0"


def pair_follows(argv, flag, value):
    """True when `flag` appears immediately followed by `value`."""
    return any(argv[i] == flag and argv[i + 1] == value
               for i in range(len(argv) - 1))


def test_dshow_input_uses_camera_device_name():
    argv = build_capture_command(dshow_cam(), URL)
    assert pair_follows(argv, "-f", "dshow")
    assert pair_follows(argv, "-i", "video=Logitech BRIO")
    assert pair_follows(argv, "-video_size", "1920x1080")
    assert pair_follows(argv, "-framerate", "30")


def test_hardware_mjpeg_decode_keeps_frames_in_vram():
    argv = build_capture_command(dshow_cam(), URL, hw_mjpeg_decode=True)
    assert pair_follows(argv, "-c:v", "mjpeg_cuvid")
    assert pair_follows(argv, "-hwaccel_output_format", "cuda")
    # decoder must be declared before the input it applies to
    assert argv.index("mjpeg_cuvid") < argv.index("-i")


def test_software_fallback_selects_mjpeg_without_cuda():
    argv = build_capture_command(dshow_cam(), URL, hw_mjpeg_decode=False)
    assert "mjpeg_cuvid" not in argv
    assert "-hwaccel_output_format" not in argv
    assert pair_follows(argv, "-vcodec", "mjpeg")


def test_encoder_flags_match_spec():
    argv = build_capture_command(dshow_cam(), URL)
    assert pair_follows(argv, "-c:v", "h264_nvenc")
    assert pair_follows(argv, "-preset", "p1")
    assert pair_follows(argv, "-tune", "ull")
    assert pair_follows(argv, "-b:v", "8M")
    assert pair_follows(argv, "-bf", "0")
    assert pair_follows(argv, "-g", "30")
    assert pair_follows(argv, "-rc", "cbr")
    assert pair_follows(argv, "-delay", "0")


def test_output_is_rtsp_over_tcp():
    argv = build_capture_command(dshow_cam(), URL)
    assert pair_follows(argv, "-f", "rtsp")
    assert pair_follows(argv, "-rtsp_transport", "tcp")
    assert argv[-1] == URL


def test_never_emits_wallclock_timestamps():
    """Spec forbids it: it stamps arrival time, not capture time."""
    argv = build_capture_command(dshow_cam(), URL)
    assert "-use_wallclock_as_timestamps" not in argv


def test_nvenc_only_flags_omitted_for_software_encoder():
    cam = dshow_cam(codec="libx264", preset="ultrafast",
                    tune="zerolatency", bitrate="8M", gop=30)
    argv = build_capture_command(cam, URL)
    assert "-rc" not in argv
    assert "-delay" not in argv
    assert pair_follows(argv, "-preset", "ultrafast")
    assert pair_follows(argv, "-tune", "zerolatency")


def test_lavfi_source_builds_synthetic_input():
    cam = CameraConfig(id="cam0", source=SourceConfig(type="lavfi"),
                       video=VideoConfig(1920, 1080, 30),
                       encode=EncodeConfig(codec="libx264",
                                           preset="ultrafast",
                                           tune="zerolatency"))
    argv = build_capture_command(cam, URL)
    assert pair_follows(argv, "-f", "lavfi")
    assert pair_follows(argv, "-i", "testsrc2=size=1920x1080:rate=30")
    assert "dshow" not in argv


def test_unknown_source_type_raises():
    cam = CameraConfig(id="cam0", source=SourceConfig(type="telepathy"),
                       video=VideoConfig(), encode=EncodeConfig())
    with pytest.raises(ValueError, match="telepathy"):
        build_capture_command(cam, URL)


def test_command_string_quotes_device_names_containing_spaces():
    argv = build_capture_command(dshow_cam(), URL)
    assert '"video=Logitech BRIO"' in command_string(argv)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ffmpeg_cmd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.ffmpeg_cmd'`

- [ ] **Step 3: Implement**

`src/anycam/ffmpeg_cmd.py`:

```python
from __future__ import annotations

from .config import CameraConfig


def build_capture_command(camera: CameraConfig, rtsp_url: str, *,
                          hw_mjpeg_decode: bool = True) -> list[str]:
    """Build the ffmpeg argv that captures one camera and publishes it.

    Cameras negotiate MJPEG at 1080p30 because USB 2.0 cannot carry raw video
    at that rate, so a transcode to H.264 is unavoidable. Decoding through
    `mjpeg_cuvid` with a CUDA output format keeps the frame in VRAM from
    decode through encode, avoiding any host copy.
    """
    v, e, s = camera.video, camera.encode, camera.source
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]

    if s.type == "dshow":
        argv += ["-f", "dshow", "-rtbufsize", "64M",
                 "-framerate", str(v.fps),
                 "-video_size", f"{v.width}x{v.height}"]
        if hw_mjpeg_decode:
            argv += ["-c:v", "mjpeg_cuvid", "-hwaccel_output_format", "cuda"]
        else:
            argv += ["-vcodec", "mjpeg"]
        argv += ["-i", f"video={s.device}"]
    elif s.type == "lavfi":
        argv += ["-f", "lavfi",
                 "-i", f"testsrc2=size={v.width}x{v.height}:rate={v.fps}"]
    else:
        raise ValueError(f"unsupported source type {s.type!r}")

    argv += ["-c:v", e.codec, "-preset", e.preset, "-tune", e.tune,
             "-b:v", e.bitrate, "-bf", "0", "-g", str(e.gop)]
    if e.codec.endswith("_nvenc"):
        argv += ["-rc", "cbr", "-delay", "0"]

    argv += ["-f", "rtsp", "-rtsp_transport", "tcp", rtsp_url]
    return argv


def command_string(argv: list[str]) -> str:
    """Render argv as a command line for MediaMTX's runOnInit (Windows shell)."""
    return " ".join(f'"{a}"' if " " in a else a for a in argv)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ffmpeg_cmd.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/anycam/ffmpeg_cmd.py tests/test_ffmpeg_cmd.py
git commit -m "feat: build ffmpeg capture commands for dshow and lavfi sources"
```

---

### Task 6: MediaMTX configuration generation

**Files:**
- Create: `src/anycam/mediamtx.py`
- Test: `tests/test_mediamtx.py`

**Interfaces:**
- Consumes: `AppConfig` (Task 1), `build_capture_command` / `command_string` (Task 5).
- Produces: `render_mediamtx_config(config, *, hw_mjpeg_decode=True) -> str`, `write_mediamtx_config(config, path, *, hw_mjpeg_decode=True) -> Path`.

MediaMTX owns supervision layer 1: each camera is a path whose `runOnInit` is
that camera's ffmpeg command, with `runOnInitRestart` turning "ffmpeg died" into
"ffmpeg restarts" without any code of ours.

- [ ] **Step 1: Write the failing test**

`tests/test_mediamtx.py`:

```python
import yaml

from anycam.config import (AppConfig, CameraConfig, ClientConfig, EncodeConfig,
                           ServerConfig, SourceConfig, VideoConfig)
from anycam.mediamtx import render_mediamtx_config


def app_config(n=2):
    cams = tuple(
        CameraConfig(id=f"cam{i}",
                     source=SourceConfig(type="dshow", device=f"Camera {i}"),
                     video=VideoConfig(1920, 1080, 30),
                     encode=EncodeConfig())
        for i in range(n))
    return AppConfig(server=ServerConfig(rtsp_port=8554),
                     cameras=cams, client=ClientConfig())


def test_one_path_per_camera():
    doc = yaml.safe_load(render_mediamtx_config(app_config(3)))
    assert sorted(doc["paths"]) == ["cam0", "cam1", "cam2"]


def test_rtsp_port_comes_from_config():
    doc = yaml.safe_load(render_mediamtx_config(app_config()))
    assert doc["rtspAddress"] == ":8554"


def test_each_path_runs_its_own_ffmpeg_and_restarts_it():
    doc = yaml.safe_load(render_mediamtx_config(app_config()))
    cam0 = doc["paths"]["cam0"]
    assert "ffmpeg" in cam0["runOnInit"]
    assert '"video=Camera 0"' in cam0["runOnInit"]
    assert cam0["runOnInitRestart"] is True


def test_each_camera_publishes_to_its_own_path():
    doc = yaml.safe_load(render_mediamtx_config(app_config()))
    assert doc["paths"]["cam0"]["runOnInit"].endswith("/cam0")
    assert doc["paths"]["cam1"]["runOnInit"].endswith("/cam1")


def test_software_fallback_propagates_to_generated_commands():
    doc = yaml.safe_load(
        render_mediamtx_config(app_config(), hw_mjpeg_decode=False))
    assert "mjpeg_cuvid" not in doc["paths"]["cam0"]["runOnInit"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mediamtx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.mediamtx'`

- [ ] **Step 3: Implement**

`src/anycam/mediamtx.py`:

```python
from __future__ import annotations

from pathlib import Path

import yaml

from .config import AppConfig
from .ffmpeg_cmd import build_capture_command, command_string


def render_mediamtx_config(config: AppConfig, *,
                           hw_mjpeg_decode: bool = True) -> str:
    """Render mediamtx.yml: one path per camera, each running its own ffmpeg.

    `runOnInitRestart` provides supervision layer 1 — a camera whose ffmpeg
    dies (unplugged, driver fault) is restarted by MediaMTX without any
    involvement from our code, and without affecting other paths.
    """
    paths = {}
    for cam in config.cameras:
        url = f"rtsp://127.0.0.1:{config.server.rtsp_port}/{cam.id}"
        argv = build_capture_command(cam, url,
                                     hw_mjpeg_decode=hw_mjpeg_decode)
        paths[cam.id] = {
            "runOnInit": command_string(argv),
            "runOnInitRestart": True,
        }

    doc = {
        "rtspAddress": f":{config.server.rtsp_port}",
        "protocols": ["tcp"],
        "logLevel": "info",
        "paths": paths,
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def write_mediamtx_config(config: AppConfig, path: str | Path, *,
                          hw_mjpeg_decode: bool = True) -> Path:
    out = Path(path)
    out.write_text(render_mediamtx_config(
        config, hw_mjpeg_decode=hw_mjpeg_decode))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mediamtx.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/anycam/mediamtx.py tests/test_mediamtx.py
git commit -m "feat: generate MediaMTX config with per-camera ffmpeg supervision"
```

---

### Task 7: RTSP receiver with reconnect

**Files:**
- Create: `src/anycam/receiver.py`
- Test: `tests/test_receiver.py`

**Interfaces:**
- Consumes: `Frame`, `ClientConfig` (Task 1), `LatestFrameBuffer` (Task 3), `Backoff`, `Watchdog` (Task 4).
- Produces: `LOW_LATENCY_OPTIONS: dict[str, str]`, `CameraReceiver(camera_id, url, client_config, *, opener, buffer=None, clock=time.monotonic_ns)` with `.start()`, `.stop()`, `.force_reconnect()`, `.buffer`, `.watchdog`, `.reconnects: int`, `.frames: int`.

The container opener is injected, so the entire reconnect state machine is
tested without a network, a server, or a camera.

- [ ] **Step 1: Write the failing test**

`tests/test_receiver.py`:

```python
import threading
import time

from anycam.config import ClientConfig
from anycam.receiver import LOW_LATENCY_OPTIONS, CameraReceiver


class FakeVideoFrame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format="bgr24"):
        return [[0]]


class FakeContainer:
    """Yields `count` frames, then behaves per `then`: 'end' | 'hang' | 'raise'."""

    def __init__(self, count=3, then="end"):
        self._count = count
        self._then = then
        self.closed = False
        self._released = threading.Event()

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            yield FakeVideoFrame(pts=i * 3000)
        if self._then == "raise":
            raise OSError("stream broke")
        if self._then == "hang":
            self._released.wait(timeout=5)
            raise OSError("closed while blocked")

    def close(self):
        self.closed = True
        self._released.set()


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


FAST = ClientConfig()


def test_low_latency_options_match_spec():
    assert LOW_LATENCY_OPTIONS == {
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
        "flags": "low_delay",
        "probesize": "32",
        "analyzeduration": "0",
        "reorder_queue_size": "0",
        "max_delay": "0",
    }


def test_opener_receives_url_and_low_latency_options():
    seen = {}

    def opener(url, options):
        seen["url"] = url
        seen["options"] = options
        return FakeContainer(count=1)

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    wait_for(lambda: rx.frames >= 1)
    rx.stop()

    assert seen["url"] == "rtsp://h/cam0"
    assert seen["options"] == LOW_LATENCY_OPTIONS


def test_decoded_frames_land_in_the_buffer_with_identity():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=5))
    rx.start()
    assert wait_for(lambda: rx.frames >= 5)
    rx.stop()

    frame = rx.buffer.get()
    assert frame.camera_id == "cam0"
    assert frame.frame_id >= 1
    assert frame.pts is not None
    assert frame.recv_ns > 0


def test_reconnects_after_the_stream_ends():
    opened = []

    def opener(url, options):
        opened.append(url)
        return FakeContainer(count=1, then="end")

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: len(opened) >= 3)
    rx.stop()
    assert rx.reconnects >= 2


def test_reconnects_when_the_opener_raises():
    attempts = []

    def opener(url, options):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return FakeContainer(count=2)

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert len(attempts) >= 3


def test_backoff_resets_after_a_successful_frame():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=1))
    rx.start()
    assert wait_for(lambda: rx.frames >= 3)
    rx.stop()
    # Each cycle delivered a frame, so backoff never escalated past initial.
    assert rx.backoff.attempts <= 1


def test_force_reconnect_closes_a_hung_container():
    containers = []

    def opener(url, options):
        c = FakeContainer(count=1, then="hang")
        containers.append(c)
        return c

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST, opener=opener)
    rx.start()
    assert wait_for(lambda: rx.frames >= 1)
    rx.force_reconnect()
    assert wait_for(lambda: containers[0].closed)
    assert wait_for(lambda: len(containers) >= 2)
    rx.stop()


def test_watchdog_beats_on_every_frame():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=3))
    rx.start()
    assert wait_for(lambda: rx.frames >= 3)
    assert rx.watchdog.age_s() < 1.0
    rx.stop()


def test_stop_terminates_the_thread():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda url, options: FakeContainer(count=100))
    rx.start()
    wait_for(lambda: rx.frames >= 1)
    rx.stop()
    assert not rx.is_alive()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_receiver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.receiver'`

- [ ] **Step 3: Implement**

`src/anycam/receiver.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .backoff import Backoff
from .config import ClientConfig
from .frame import Frame
from .freshness import LatestFrameBuffer
from .watchdog import Watchdog

log = logging.getLogger(__name__)

# Mandatory. The RTSP demuxer's jitter buffer can silently add more delay than
# the entire rest of the pipeline, and it is invisible without measurement.
LOW_LATENCY_OPTIONS: dict[str, str] = {
    "rtsp_transport": "tcp",
    "fflags": "nobuffer",
    "flags": "low_delay",
    "probesize": "32",
    "analyzeduration": "0",
    "reorder_queue_size": "0",
    "max_delay": "0",
}


def default_opener(url: str, options: dict[str, str]) -> Any:
    import av
    return av.open(url, options=options)


class CameraReceiver(threading.Thread):
    """Receives one camera's RTSP stream, decoding into a newest-frame buffer.

    Runs forever: a closed stream, a refused connection or a forced reconnect
    are all normal states that lead back to `connect()`, never to exit.
    """

    def __init__(self, camera_id: str, url: str, client_config: ClientConfig, *,
                 opener: Callable[[str, dict[str, str]], Any] = default_opener,
                 buffer: LatestFrameBuffer | None = None,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        super().__init__(name=f"receiver-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.url = url
        self.buffer = buffer if buffer is not None else LatestFrameBuffer()
        self.watchdog = Watchdog(client_config.watchdog_timeout_s, clock=clock)
        self.backoff = Backoff(client_config.backoff)
        self.frames = 0
        self.reconnects = 0
        self.last_error: str | None = None

        self._opener = opener
        self._clock = clock
        self._stop = threading.Event()
        self._container_lock = threading.Lock()
        self._container: Any | None = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._close_container()
        self.join(timeout=timeout)

    def force_reconnect(self) -> None:
        """Close the container so a blocked read raises and the loop retries.

        A wedged stream cannot be interrupted from inside the decode loop,
        because the read never returns. Closing from outside is what unblocks
        it, which is why the watchdog lives outside this thread.
        """
        log.warning("%s: forcing reconnect", self.camera_id)
        self._close_container()

    def _close_container(self) -> None:
        with self._container_lock:
            container, self._container = self._container, None
        if container is not None:
            try:
                container.close()
            except Exception:
                log.debug("%s: error closing container", self.camera_id,
                          exc_info=True)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                container = self._opener(self.url, LOW_LATENCY_OPTIONS)
            except Exception as exc:
                self.last_error = str(exc)
                self.reconnects += 1
                self._wait(self.backoff.next_delay())
                continue

            with self._container_lock:
                self._container = container
            try:
                self._consume(container)
            except Exception as exc:
                self.last_error = str(exc)
                log.info("%s: stream ended: %s", self.camera_id, exc)
            finally:
                self._close_container()

            if not self._stop.is_set():
                self.reconnects += 1
                self._wait(self.backoff.next_delay())

    def _consume(self, container: Any) -> None:
        for decoded in container.decode(video=0):
            if self._stop.is_set():
                return
            recv_ns = self._clock()
            image = decoded.to_ndarray(format="bgr24")
            self.frames += 1
            self.buffer.put(Frame(
                camera_id=self.camera_id,
                frame_id=self.frames,
                pts=decoded.pts,
                recv_ns=recv_ns,
                decoded_ns=self._clock(),
                image=image,
            ))
            self.watchdog.beat()
            self.backoff.reset()

    def _wait(self, seconds: float) -> None:
        self._stop.wait(timeout=seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_receiver.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Write the failing sender-clock test**

The spec's middle timing layer: derive a capture-time *estimate* from the
sender's clock. Containers that do not expose stream timing (including every
fake in these tests) must yield `None` rather than a fabricated number — a
wrong timestamp here is worse than an absent one.

Append to `tests/test_receiver.py`:

```python
from fractions import Fraction

from anycam.receiver import estimate_capture_ns


def test_estimate_uses_sender_start_time_and_pts():
    # start_time_realtime is microseconds since epoch; pts is in time_base units
    est = estimate_capture_ns(start_time_realtime=1_700_000_000_000_000,
                              pts=90000, time_base=Fraction(1, 90000))
    assert est == 1_700_000_000_000_000 * 1000 + 1_000_000_000


def test_estimate_is_none_without_a_sender_clock():
    assert estimate_capture_ns(None, 90000, Fraction(1, 90000)) is None


def test_estimate_is_none_without_pts():
    assert estimate_capture_ns(1_700_000_000_000_000, None,
                               Fraction(1, 90000)) is None


def test_estimate_is_none_without_a_time_base():
    assert estimate_capture_ns(1_700_000_000_000_000, 90000, None) is None


def test_receiver_populates_estimate_when_container_exposes_timing():
    class FakeStream:
        time_base = Fraction(1, 90000)

    class FakeStreams:
        video = [FakeStream()]

    class TimedContainer(FakeContainer):
        streams = FakeStreams()
        start_time_realtime = 1_700_000_000_000_000

    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda u, o: TimedContainer(count=2))
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert rx.buffer.get().est_capture_ns is not None


def test_receiver_leaves_estimate_none_when_timing_is_unavailable():
    rx = CameraReceiver("cam0", "rtsp://h/cam0", FAST,
                        opener=lambda u, o: FakeContainer(count=2))
    rx.start()
    assert wait_for(lambda: rx.frames >= 2)
    rx.stop()
    assert rx.buffer.get().est_capture_ns is None
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_receiver.py -v`
Expected: FAIL — `ImportError: cannot import name 'estimate_capture_ns'`

- [ ] **Step 7: Implement the sender-clock estimate**

Add to `src/anycam/receiver.py`:

```python
def estimate_capture_ns(start_time_realtime: int | None, pts: int | None,
                        time_base: Any | None) -> int | None:
    """Estimate capture wall-clock from the sender's clock, or None.

    `start_time_realtime` is microseconds since the epoch, as reported by the
    container via RTCP sender reports. This is explicitly an estimate: the
    spec treats the authoritative latency measurement as the out-of-band
    calibration harness, because no in-band timestamp can account for sensor
    exposure and USB transfer.
    """
    if start_time_realtime is None or pts is None or time_base is None:
        return None
    return int(start_time_realtime) * 1000 + int(pts * float(time_base) * 1e9)


def _sender_clock(container: Any) -> tuple[int | None, Any | None]:
    """Read (start_time_realtime, time_base), tolerating containers without them."""
    streams = getattr(container, "streams", None)
    video = getattr(streams, "video", None) if streams is not None else None
    if not video:
        return None, None
    return (getattr(container, "start_time_realtime", None),
            getattr(video[0], "time_base", None))
```

Then use it in `_consume`, reading the clock once per container rather than
per frame:

```python
    def _consume(self, container: Any) -> None:
        start_realtime, time_base = _sender_clock(container)
        for decoded in container.decode(video=0):
            if self._stop.is_set():
                return
            recv_ns = self._clock()
            image = decoded.to_ndarray(format="bgr24")
            self.frames += 1
            self.buffer.put(Frame(
                camera_id=self.camera_id,
                frame_id=self.frames,
                pts=decoded.pts,
                recv_ns=recv_ns,
                decoded_ns=self._clock(),
                image=image,
                est_capture_ns=estimate_capture_ns(
                    start_realtime, decoded.pts, time_base),
            ))
            self.watchdog.beat()
            self.backoff.reset()
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_receiver.py -v`
Expected: PASS (15 tests)

- [ ] **Step 9: Commit**

```bash
git add src/anycam/receiver.py tests/test_receiver.py
git commit -m "feat: RTSP receiver with low-latency options, reconnect and capture estimate"
```

---

### Task 8: Multi-camera client and isolation

**Files:**
- Create: `src/anycam/stats.py`
- Create: `src/anycam/client.py`
- Test: `tests/test_stats.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `AppConfig` (Task 1), `CameraReceiver` (Task 7).
- Produces: `StreamStats` dataclass; `MultiCameraClient(config, host, *, opener=default_opener, monitor_interval_s=0.25)` with `.start()`, `.stop()`, `.latest(camera_id) -> Frame | None`, `.take(camera_id) -> Frame | None`, `.stats() -> dict[str, StreamStats]`, `.camera_ids: tuple[str, ...]`, and context-manager support.

The monitor thread lives here rather than in the receiver, because the
watchdog must be able to act on a receiver blocked inside a read.

- [ ] **Step 1: Write the failing stats test**

`tests/test_stats.py`:

```python
from anycam.stats import StreamStats


def test_format_line_includes_the_silent_failure_signals():
    line = StreamStats(camera_id="cam0", frames=900, dropped=612,
                       reconnects=2, age_s=0.033, depth=1,
                       last_error=None).format_line()
    assert "cam0" in line
    assert "900" in line
    assert "612" in line
    assert "2" in line


def test_stalled_stream_is_reported_as_stalled():
    stats = StreamStats(camera_id="cam0", frames=10, dropped=0, reconnects=0,
                        age_s=9.5, depth=0, last_error=None)
    assert stats.is_stalled(timeout_s=2.0)
    assert "STALLED" in stats.format_line(timeout_s=2.0)


def test_healthy_stream_is_not_stalled():
    stats = StreamStats(camera_id="cam0", frames=10, dropped=0, reconnects=0,
                        age_s=0.02, depth=1, last_error=None)
    assert not stats.is_stalled(timeout_s=2.0)
    assert "STALLED" not in stats.format_line(timeout_s=2.0)
```

- [ ] **Step 2: Write the failing client test**

`tests/test_client.py`:

```python
import threading
import time

from anycam.config import (AppConfig, CameraConfig, ClientConfig, EncodeConfig,
                           ServerConfig, SourceConfig, VideoConfig)
from anycam.client import MultiCameraClient


def app_config(n=3):
    cams = tuple(
        CameraConfig(id=f"cam{i}",
                     source=SourceConfig(type="dshow", device=f"Camera {i}"),
                     video=VideoConfig(), encode=EncodeConfig())
        for i in range(n))
    return AppConfig(server=ServerConfig(rtsp_port=8554),
                     cameras=cams, client=ClientConfig())


class FakeVideoFrame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format="bgr24"):
        return [[0]]


class FakeContainer:
    def __init__(self, count=1000, stall_after=None):
        self._count = count
        self._stall_after = stall_after
        self.closed = False
        self._released = threading.Event()

    def decode(self, video=0):
        for i in range(self._count):
            if self.closed:
                return
            if self._stall_after is not None and i >= self._stall_after:
                self._released.wait(timeout=10)
                raise OSError("closed while stalled")
            yield FakeVideoFrame(pts=i * 3000)
            time.sleep(0.002)

    def close(self):
        self.closed = True
        self._released.set()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_starts_one_receiver_per_camera():
    with MultiCameraClient(app_config(3), host="172.30.64.1",
                           opener=lambda u, o: FakeContainer()) as client:
        assert client.camera_ids == ("cam0", "cam1", "cam2")
        assert wait_for(lambda: all(
            client.latest(c) is not None for c in client.camera_ids))


def test_builds_rtsp_urls_from_the_discovered_host():
    seen = []

    def opener(url, options):
        seen.append(url)
        return FakeContainer()

    with MultiCameraClient(app_config(2), host="172.30.64.1",
                           opener=opener):
        wait_for(lambda: len(seen) >= 2)

    assert "rtsp://172.30.64.1:8554/cam0" in seen
    assert "rtsp://172.30.64.1:8554/cam1" in seen


def test_latest_returns_the_newest_frame_for_that_camera():
    with MultiCameraClient(app_config(2), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        first = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > first)


def test_take_consumes_so_the_next_frame_is_not_a_drop():
    with MultiCameraClient(app_config(1), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        assert wait_for(lambda: client.take("cam0") is not None)
        assert client.latest("cam0") is None


def test_one_dead_camera_does_not_affect_the_others():
    """The property the whole architecture exists to provide."""
    def opener(url, options):
        if url.endswith("cam1"):
            raise OSError("camera 1 is unplugged")
        return FakeContainer()

    with MultiCameraClient(app_config(3), host="h", opener=opener) as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        assert wait_for(lambda: client.latest("cam2") is not None)
        assert client.latest("cam1") is None
        assert client.stats()["cam1"].reconnects >= 1

        healthy = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > healthy)


def test_monitor_forces_reconnect_on_a_wedged_stream():
    """A wedged camera holds a healthy connection; only the watchdog sees it."""
    cfg = app_config(1)
    cfg = AppConfig(server=cfg.server, cameras=cfg.cameras,
                    client=ClientConfig(watchdog_timeout_s=0.2))
    containers = []

    def opener(url, options):
        c = FakeContainer(stall_after=2)
        containers.append(c)
        return c

    with MultiCameraClient(cfg, host="h", opener=opener,
                           monitor_interval_s=0.02) as client:
        assert wait_for(lambda: len(containers) >= 2, timeout=5.0)
        assert containers[0].closed
        assert client.stats()["cam0"].reconnects >= 1


def test_stats_reports_every_camera():
    with MultiCameraClient(app_config(3), host="h",
                           opener=lambda u, o: FakeContainer()) as client:
        wait_for(lambda: client.latest("cam2") is not None)
        stats = client.stats()
        assert sorted(stats) == ["cam0", "cam1", "cam2"]
        assert stats["cam0"].frames > 0


def test_stop_is_idempotent():
    client = MultiCameraClient(app_config(2), host="h",
                               opener=lambda u, o: FakeContainer())
    client.start()
    client.stop()
    client.stop()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stats.py tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError` for `anycam.stats` and `anycam.client`

- [ ] **Step 4: Implement stats**

`src/anycam/stats.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamStats:
    """Per-camera health. Observability is load-bearing here, not a nicety:
    every failure mode in this system is silent."""

    camera_id: str
    frames: int
    dropped: int
    reconnects: int
    age_s: float
    depth: int
    last_error: str | None

    def is_stalled(self, timeout_s: float) -> bool:
        return self.age_s > timeout_s

    def format_line(self, timeout_s: float = 2.0) -> str:
        state = "STALLED" if self.is_stalled(timeout_s) else "ok"
        return (f"{self.camera_id:<8} {state:<8} "
                f"frames={self.frames:<8} dropped={self.dropped:<8} "
                f"reconnects={self.reconnects:<4} age={self.age_s:6.3f}s "
                f"depth={self.depth}")
```

- [ ] **Step 5: Implement the client**

`src/anycam/client.py`:

```python
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .config import AppConfig
from .frame import Frame
from .receiver import CameraReceiver, default_opener
from .stats import StreamStats

log = logging.getLogger(__name__)


class MultiCameraClient:
    """Owns one independent receiver per camera plus a shared monitor thread.

    Cameras share no state. A camera that dies, wedges or never appears
    affects only its own receiver, buffer, watchdog and backoff.
    """

    def __init__(self, config: AppConfig, host: str, *,
                 opener: Callable[[str, dict[str, str]], Any] = default_opener,
                 monitor_interval_s: float = 0.25) -> None:
        self._config = config
        self._host = host
        self._monitor_interval_s = monitor_interval_s
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._receivers: dict[str, CameraReceiver] = {
            cam.id: CameraReceiver(
                cam.id, self._url(cam.id), config.client, opener=opener)
            for cam in config.cameras
        }

    def _url(self, camera_id: str) -> str:
        return f"rtsp://{self._host}:{self._config.server.rtsp_port}/{camera_id}"

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._receivers)

    def start(self) -> None:
        for rx in self._receivers.values():
            rx.start()
        self._monitor = threading.Thread(
            target=self._run_monitor, name="watchdog-monitor", daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5.0)
            self._monitor = None
        for rx in self._receivers.values():
            if rx.is_alive():
                rx.stop()

    def latest(self, camera_id: str) -> Frame | None:
        return self._receivers[camera_id].buffer.get()

    def take(self, camera_id: str) -> Frame | None:
        return self._receivers[camera_id].buffer.take()

    def stats(self) -> dict[str, StreamStats]:
        return {
            cid: StreamStats(
                camera_id=cid,
                frames=rx.frames,
                dropped=rx.buffer.dropped,
                reconnects=rx.reconnects,
                age_s=rx.watchdog.age_s(),
                depth=rx.buffer.depth,
                last_error=rx.last_error,
            )
            for cid, rx in self._receivers.items()
        }

    def _run_monitor(self) -> None:
        """Force a reconnect on any stream whose watchdog has expired.

        This cannot live inside the receiver: a wedged stream is blocked
        inside a read that never returns, so only another thread can close
        the container and unblock it.
        """
        while not self._stop.wait(self._monitor_interval_s):
            for cid, rx in self._receivers.items():
                if rx.watchdog.expired():
                    log.warning("%s: no frames for %.2fs, reconnecting",
                                cid, rx.watchdog.age_s())
                    rx.watchdog.beat()
                    rx.force_reconnect()

    def __enter__(self) -> MultiCameraClient:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stats.py tests/test_client.py -v`
Expected: PASS (11 tests)

- [ ] **Step 7: Run the whole unit suite**

Run: `.venv/bin/pytest tests -v -m "not integration"`
Expected: PASS, ~60 tests, under 30 seconds

- [ ] **Step 8: Commit**

```bash
git add src/anycam/stats.py src/anycam/client.py tests/test_stats.py tests/test_client.py
git commit -m "feat: multi-camera client with per-camera isolation and watchdog monitor"
```

---

### Task 9: Command-line interface

**Files:**
- Create: `src/anycam/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None) -> int`; subcommands `serve-config`, `watch`, `devices`.

`serve-config` generates `mediamtx.yml` on Windows. `watch` runs the WSL client
and prints per-camera stats. `devices` wraps DirectShow enumeration, whose
non-zero exit code is expected and must not be reported as failure.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import yaml

from anycam.cli import main

CONFIG = """
server: {rtsp_port: 8554}
cameras:
  - id: cam0
    source: {type: dshow, device: "Logitech BRIO"}
    video: {width: 1920, height: 1080, fps: 30}
    encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}
"""


def test_serve_config_writes_mediamtx_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    out = tmp_path / "mediamtx.yml"

    assert main(["serve-config", "-c", str(cfg), "-o", str(out)]) == 0

    doc = yaml.safe_load(out.read_text())
    assert "cam0" in doc["paths"]
    assert doc["paths"]["cam0"]["runOnInitRestart"] is True


def test_serve_config_honours_software_fallback(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    out = tmp_path / "mediamtx.yml"

    main(["serve-config", "-c", str(cfg), "-o", str(out), "--no-hw-mjpeg"])

    doc = yaml.safe_load(out.read_text())
    assert "mjpeg_cuvid" not in doc["paths"]["cam0"]["runOnInit"]


def test_invalid_config_reports_error_not_traceback(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("cameras: []\n")

    assert main(["serve-config", "-c", str(cfg),
                 "-o", str(tmp_path / "m.yml")]) == 2
    assert "at least one camera" in capsys.readouterr().err


def test_missing_config_file_reports_error(tmp_path, capsys):
    assert main(["serve-config", "-c", str(tmp_path / "nope.yaml"),
                 "-o", str(tmp_path / "m.yml")]) == 2
    assert "nope.yaml" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anycam.cli'`

- [ ] **Step 3: Implement**

`src/anycam/cli.py`:

```python
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

from .config import ConfigError, load_config
from .host import HostDiscoveryError, discover_windows_host
from .mediamtx import write_mediamtx_config


def _serve_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    out = write_mediamtx_config(config, args.output,
                                hw_mjpeg_decode=not args.no_hw_mjpeg)
    print(f"wrote {out} ({len(config.cameras)} camera(s))")
    print(f"run on Windows:  mediamtx.exe {out}")
    return 0


def _watch(args: argparse.Namespace) -> int:
    from .client import MultiCameraClient

    config = load_config(args.config)
    host = args.host or discover_windows_host()
    print(f"connecting to {host}:{config.server.rtsp_port}")

    with MultiCameraClient(config, host) as client:
        try:
            while True:
                time.sleep(args.interval)
                print(f"--- {time.strftime('%H:%M:%S')} ---")
                for stats in client.stats().values():
                    print(stats.format_line(config.client.watchdog_timeout_s))
        except KeyboardInterrupt:
            return 0


def _devices(args: argparse.Namespace) -> int:
    """List DirectShow devices.

    ffmpeg exits non-zero here by design and prints the list to stderr; that
    is not a failure and must not be reported as one.
    """
    proc = subprocess.run(
        [args.ffmpeg, "-hide_banner", "-list_devices", "true",
         "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True)
    sys.stdout.write(proc.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anycam")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve-config", help="generate mediamtx.yml (Windows)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-o", "--output", default="mediamtx.yml")
    p.add_argument("--no-hw-mjpeg", action="store_true",
                   help="fall back to CPU MJPEG decode (no mjpeg_cuvid)")
    p.set_defaults(func=_serve_config)

    p = sub.add_parser("watch", help="consume streams in WSL and print stats")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--host", default=None,
                   help="Windows host (default: discover from default route)")
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=_watch)

    p = sub.add_parser("devices", help="list DirectShow capture devices")
    p.add_argument("--ffmpeg", default="ffmpeg.exe")
    p.set_defaults(func=_devices)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        return args.func(args)
    except (ConfigError, HostDiscoveryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/anycam/cli.py tests/test_cli.py
git commit -m "feat: CLI for config generation, stream watching and device listing"
```

---

### Task 10: End-to-end integration test with a synthetic camera

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_synthetic_stream.py`

**Interfaces:**
- Consumes: `AppConfig` (Task 1), `render_mediamtx_config` (Task 6), `MultiCameraClient` (Task 8).
- Produces: pytest fixtures `binaries`, `synthetic_stack`.

This exercises the real transport — real ffmpeg, real MediaMTX, real RTSP, real
PyAV decode — with only DirectShow stubbed out. It runs on plain Linux with
`libx264`, so it validates all protocol and supervision logic without a GPU.

- [ ] **Step 1: Write the fixtures**

`tests/integration/__init__.py` — empty file.

`tests/integration/conftest.py`:

```python
import shutil
import socket
import subprocess
import time

import pytest

from anycam.config import (AppConfig, CameraConfig, ClientConfig, EncodeConfig,
                           ServerConfig, SourceConfig, VideoConfig)
from anycam.mediamtx import write_mediamtx_config


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def binaries():
    missing = [b for b in ("ffmpeg", "mediamtx") if shutil.which(b) is None]
    if missing:
        pytest.skip(f"missing binaries: {', '.join(missing)}")
    return True


@pytest.fixture
def synthetic_stack(binaries, tmp_path):
    """Two synthetic cameras published through a real MediaMTX instance.

    Software x264 is used so this runs anywhere, including CI without a GPU.
    """
    port = free_port()
    cameras = tuple(
        CameraConfig(id=f"cam{i}", source=SourceConfig(type="lavfi"),
                     video=VideoConfig(640, 480, 30),
                     encode=EncodeConfig(codec="libx264", preset="ultrafast",
                                         tune="zerolatency", bitrate="2M",
                                         gop=30))
        for i in range(2))
    config = AppConfig(server=ServerConfig(rtsp_port=port), cameras=cameras,
                       client=ClientConfig(watchdog_timeout_s=3.0))

    cfg_path = write_mediamtx_config(config, tmp_path / "mediamtx.yml",
                                     hw_mjpeg_decode=False)
    proc = subprocess.Popen([shutil.which("mediamtx"), str(cfg_path)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail(f"mediamtx did not listen on {port}")

    try:
        yield config, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 2: Write the failing integration test**

`tests/integration/test_synthetic_stream.py`:

```python
import time

import pytest

from anycam.client import MultiCameraClient

pytestmark = pytest.mark.integration


def wait_for(predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_frames_arrive_from_both_synthetic_cameras(synthetic_stack):
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        assert wait_for(lambda: client.latest("cam1") is not None)

        frame = client.latest("cam0")
        assert frame.image.shape == (480, 640, 3)
        assert frame.camera_id == "cam0"


def test_frames_advance_over_time(synthetic_stack):
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        first = client.latest("cam0").frame_id
        assert wait_for(lambda: client.latest("cam0").frame_id > first + 30)


def test_buffer_depth_never_exceeds_one_under_a_slow_consumer(synthetic_stack):
    """A consumer slower than the stream must lose frames, not gain latency."""
    config, _ = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        for _ in range(10):
            time.sleep(0.3)          # far slower than 30fps
            assert client.stats()["cam0"].depth <= 1
        stats = client.stats()["cam0"]
        assert stats.dropped > 0, "a slow consumer must be dropping frames"


def test_client_reconnects_after_the_server_restarts(synthetic_stack):
    config, proc = synthetic_stack
    with MultiCameraClient(config, host="127.0.0.1") as client:
        assert wait_for(lambda: client.latest("cam0") is not None)
        before = client.stats()["cam0"].reconnects

        proc.terminate()
        proc.wait(timeout=10)
        assert wait_for(
            lambda: client.stats()["cam0"].reconnects > before, timeout=20)
```

- [ ] **Step 3: Install the binaries and run**

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
MEDIAMTX_VERSION=v1.9.3
curl -sL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
  | tar -xz -C /tmp mediamtx
sudo install /tmp/mediamtx /usr/local/bin/mediamtx
```

Run: `.venv/bin/pytest tests/integration -v -m integration`
Expected: PASS (4 tests). If either binary is absent the suite skips rather than fails.

- [ ] **Step 4: Commit**

```bash
git add tests/integration
git commit -m "test: end-to-end synthetic camera integration suite"
```

---

### Task 11: Latency calibration harness and bench checklist

**Files:**
- Create: `tools/calibrate_latency.py`
- Create: `docs/bench-checklist.md`
- Create: `README.md`

**Interfaces:**
- Consumes: `load_config` (Task 1), `MultiCameraClient` (Task 8), `discover_windows_host` (Task 2).
- Produces: `tools/calibrate_latency.py` as a runnable script.

No in-band timestamp can measure glass-to-inference latency, because a capture
timestamp is taken when a frame reaches software — already tens of milliseconds
after exposure. This measures the whole chain by pointing a camera at a clock.

- [ ] **Step 1: Write the calibration harness**

`tools/calibrate_latency.py`:

```python
"""Measure true glass-to-inference latency.

Procedure:
  1. On Windows, open a large millisecond clock in a browser or run
     `python tools/calibrate_latency.py display` on the Windows side.
  2. Point one camera at that screen.
  3. Run `python tools/calibrate_latency.py measure -c config.yaml --camera cam0`
     in WSL. It saves annotated frames showing the displayed time alongside
     the WSL arrival time.
  4. Read the digits off the saved frames; the difference is the total
     pipeline latency, including exposure and USB transfer.

Reading digits is deliberately manual. Automating OCR would add a dependency
and a failure mode to a procedure run a handful of times per hardware change.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam.client import MultiCameraClient          # noqa: E402
from anycam.config import load_config                # noqa: E402
from anycam.host import discover_windows_host        # noqa: E402


def display() -> int:
    """Print a millisecond clock. Run this on Windows, filmed by the camera."""
    try:
        while True:
            print(f"\r{time.strftime('%H:%M:%S')}."
                  f"{int(time.time() * 1000) % 1000:03d}", end="", flush=True)
            time.sleep(0.001)
    except KeyboardInterrupt:
        print()
        return 0


def measure(args: argparse.Namespace) -> int:
    import cv2  # only needed for the calibration path

    config = load_config(args.config)
    host = args.host or discover_windows_host()
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    with MultiCameraClient(config, host) as client:
        print(f"capturing {args.samples} samples from {args.camera}")
        captured = 0
        while captured < args.samples:
            frame = client.take(args.camera)
            if frame is None:
                time.sleep(0.01)
                continue
            wall = time.strftime("%H:%M:%S") + \
                f".{int(time.time() * 1000) % 1000:03d}"
            image = frame.image.copy()
            cv2.putText(image, f"WSL arrival: {wall}", (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            cv2.imwrite(str(outdir / f"sample_{captured:03d}.png"), image)
            captured += 1
            time.sleep(args.gap)

    print(f"wrote {captured} annotated frames to {outdir}")
    print("Read the on-screen clock in each image and subtract it from the "
          "annotated WSL arrival time. That difference is total pipeline "
          "latency, exposure and USB transfer included.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="calibrate_latency")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("display", help="run the millisecond clock (on Windows)")

    p = sub.add_parser("measure", help="capture annotated frames (in WSL)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--camera", default="cam0")
    p.add_argument("--host", default=None)
    p.add_argument("--samples", type=int, default=20)
    p.add_argument("--gap", type=float, default=0.5)
    p.add_argument("-o", "--output", default="calibration")

    args = parser.parse_args()
    return display() if args.command == "display" else measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the bench checklist**

`docs/bench-checklist.md`:

```markdown
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
- [ ] Run all cameras for 10 minutes and confirm `anycam watch` shows a stable
      frame rate on every one

## 2. DirectShow enumeration

- [ ] `anycam devices` lists every camera
- [ ] Note that ffmpeg exits non-zero here **by design** and prints to stderr;
      this is not a failure
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
- [ ] Simulate a wedged camera and confirm the watchdog forces a reconnect
      within roughly 2 seconds

## 6. Latency calibration

- [ ] Run `tools/calibrate_latency.py` per the procedure in its docstring
- [ ] Record the measured total latency per camera model
- [ ] Sanity check against the spec's budget: the camera itself is expected to
      dominate. A total far above roughly 60ms suggests the receiver's jitter
      buffer is not actually disabled.
```

- [ ] **Step 3: Write the README**

`README.md`:

```markdown
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

**WSL**:

    pip install -e ".[dev]"
    anycam watch -c config.yaml

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

## Design

See [the design spec](docs/superpowers/specs/2026-09-14-anycam-to-rtsp-design.md)
for why H.264 rather than MJPEG, why the watchdog is not redundant with
reconnect, and why latency is measured out of band.
```

- [ ] **Step 4: Verify the whole suite still passes**

Run: `.venv/bin/pytest tests -v`
Expected: PASS — unit tests plus integration tests (or skips if binaries absent)

- [ ] **Step 5: Commit**

```bash
git add tools/calibrate_latency.py docs/bench-checklist.md README.md
git commit -m "feat: latency calibration harness, bench checklist and README"
```

---

## Verification

Before declaring the work complete:

- [ ] `.venv/bin/pytest tests -m "not integration" -v` passes with no skips
- [ ] `.venv/bin/pytest tests -m integration -v` passes with ffmpeg and mediamtx installed
- [ ] `anycam serve-config` produces a `mediamtx.yml` that MediaMTX accepts
- [ ] `anycam watch` shows a stable frame rate on every camera
- [ ] Unplugging one camera leaves the others unaffected
- [ ] The bench checklist in `docs/bench-checklist.md` has been worked through
- [ ] Measured latency is consistent with the spec's budget
