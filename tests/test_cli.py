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


def test_typo_d_config_key_reports_error_not_traceback(tmp_path, capsys):
    """A typo'd key (e.g. `framerate` instead of `fps`) used to raise a raw
    TypeError that cli.py's handler did not catch, producing a full
    traceback with no `error:` line and no exit 2 -- on the single most
    likely user error."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.replace(
        "video: {width: 1920, height: 1080, fps: 30}",
        "video: {width: 1920, height: 1080, framerate: 30}"))

    assert main(["serve-config", "-c", str(cfg),
                 "-o", str(tmp_path / "m.yml")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "framerate" in err


def test_missing_config_file_reports_error(tmp_path, capsys):
    assert main(["serve-config", "-c", str(tmp_path / "nope.yaml"),
                 "-o", str(tmp_path / "m.yml")]) == 2
    assert "nope.yaml" in capsys.readouterr().err


def test_serve_config_creates_output_directory(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    out = tmp_path / "build" / "mediamtx.yml"

    assert main(["serve-config", "-c", str(cfg), "-o", str(out)]) == 0
    assert out.exists()

    doc = yaml.safe_load(out.read_text())
    assert "cam0" in doc["paths"]
