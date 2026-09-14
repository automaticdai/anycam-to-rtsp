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
