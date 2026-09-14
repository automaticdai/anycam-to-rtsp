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


def test_unused_protocols_are_disabled_but_webrtc_stays_on():
    """This system publishes and consumes RTSP only. RTMP/HLS/SRT each bind
    a fixed default port regardless of rtspAddress, so leaving them enabled
    lets an orphaned MediaMTX process squat on those ports and break the
    next instance's startup. WebRTC stays on for browser inspection."""
    doc = yaml.safe_load(render_mediamtx_config(app_config()))
    assert doc["rtmp"] is False
    assert doc["hls"] is False
    assert doc["srt"] is False
    assert doc["webrtc"] is True
