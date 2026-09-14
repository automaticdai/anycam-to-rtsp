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


def test_rejects_typo_d_key_in_video_section(tmp_path):
    """VideoConfig(**{'framerate': 30}) used to raise a raw TypeError with
    no mention of which key or section was wrong; it must become a
    ConfigError naming both."""
    text = VALID.replace("video: {width: 1920, height: 1080, fps: 30}",
                         "video: {width: 1920, height: 1080, framerate: 30}")
    with pytest.raises(ConfigError, match="framerate"):
        load_config(write(tmp_path, text))


def test_rejects_typo_d_key_in_encode_section(tmp_path):
    text = VALID.replace(
        "encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, gop: 30}",
        "encode: {codec: h264_nvenc, preset: p1, tune: ull, bitrate: 8M, "
        "keyframe_interval: 30}")
    with pytest.raises(ConfigError, match="keyframe_interval"):
        load_config(write(tmp_path, text))


def test_rejects_typo_d_key_in_client_section(tmp_path):
    text = VALID.replace(
        "client:\n  watchdog_timeout_s: 2.0\n"
        "  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}\n",
        "client:\n  watchdog_timeout: 2.0\n")
    with pytest.raises(ConfigError, match="watchdog_timeout(?!_s)"):
        load_config(write(tmp_path, text))


def test_rejects_typo_d_key_in_server_section(tmp_path):
    text = VALID.replace("server:\n  rtsp_port: 8554",
                         "server:\n  rtsp_ports: 8554")
    with pytest.raises(ConfigError, match="rtsp_ports"):
        load_config(write(tmp_path, text))


def test_rejects_typo_d_key_in_backoff_section(tmp_path):
    text = VALID.replace(
        "backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}",
        "backoff: {initial_s: 0.2, factor: 2.0, max_seconds: 5.0}")
    with pytest.raises(ConfigError, match="max_seconds"):
        load_config(write(tmp_path, text))


def test_rejects_camera_id_with_invalid_characters(tmp_path):
    """A camera id flows unchecked into a MediaMTX path key, a publish URL
    and a consume URL; a space (or other unsafe character) would break all
    three at once."""
    text = VALID.replace("id: cam0", "id: cam 0")
    with pytest.raises(ConfigError, match="cam 0"):
        load_config(write(tmp_path, text))
