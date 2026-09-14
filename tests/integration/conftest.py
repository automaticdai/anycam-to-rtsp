import os
import shutil
import signal
import socket
import subprocess
import time

import pytest
import yaml

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


def _terminate(proc: subprocess.Popen) -> None:
    """Stop `proc` and its whole process group, escalating if it won't die.

    `synthetic_stack` starts mediamtx with `start_new_session=True`, putting
    it (and, transitively, the ffmpeg children it spawns via runOnInit) in
    their own process group, so a mediamtx that does not shepherd its own
    children out in time can still be reaped in one shot. This is a normal-
    teardown safety net only: nothing can run this code if the test process
    itself is killed (e.g. a segfault) before reaching it, which is why the
    fixture also gives this instance non-default ports below -- so a leaked
    process from a crashed run has nothing fixed to collide on next time.
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wait_until_listening(port: int, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_mediamtx(cfg_path, port: int) -> subprocess.Popen:
    """Start mediamtx against `cfg_path` and wait for it to listen on
    `port`. Shared by the fixture (initial start) and any test that needs
    to restart the server mid-test (e.g. after simulating a crash)."""
    proc = subprocess.Popen([shutil.which("mediamtx"), str(cfg_path)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if not _wait_until_listening(port):
        _terminate(proc)
        pytest.fail(f"mediamtx did not listen on {port}")
    return proc


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

    # render_mediamtx_config disables RTMP/HLS/SRT outright (this system
    # never uses them), but leaves WebRTC on at MediaMTX's fixed default
    # ports (8889 HTTP, 8189 ICE/UDP) since the design relies on it for
    # browser inspection. Only one process on this host can ever bind those
    # at a time, so give this instance its own free ports instead -- a
    # leftover mediamtx from an earlier crashed run (or a second test
    # worker) must not be able to collide with this one.
    doc = yaml.safe_load(cfg_path.read_text())
    doc["webrtcAddress"] = f":{free_port()}"
    doc["webrtcLocalUDPAddress"] = f":{free_port()}"
    cfg_path.write_text(yaml.safe_dump(doc, sort_keys=False,
                                       default_flow_style=False))

    proc = _start_mediamtx(cfg_path, port)

    try:
        yield config, proc, cfg_path
    finally:
        _terminate(proc)
