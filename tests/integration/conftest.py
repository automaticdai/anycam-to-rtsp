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
