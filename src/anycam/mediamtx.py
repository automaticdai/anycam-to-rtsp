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
        # This system publishes and consumes RTSP only. RTMP/HLS/SRT each
        # bind their own fixed default port (1935/8888/8890) regardless of
        # rtspAddress, so leaving them enabled means an orphaned MediaMTX
        # process (e.g. from a crashed client) squats on those ports and
        # breaks the next instance's startup with a misleading bind error.
        # WebRTC stays enabled: the design relies on it for browser
        # inspection of a camera. A caller running more than one instance
        # at once (e.g. the integration test fixture) is responsible for
        # giving it non-default, non-colliding ports.
        "rtmp": False,
        "hls": False,
        "srt": False,
        "webrtc": True,
        "paths": paths,
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def write_mediamtx_config(config: AppConfig, path: str | Path, *,
                          hw_mjpeg_decode: bool = True) -> Path:
    out = Path(path)
    out.write_text(render_mediamtx_config(
        config, hw_mjpeg_decode=hw_mjpeg_decode))
    return out
