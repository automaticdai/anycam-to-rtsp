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


def test_scalar_config_section_reports_error_not_traceback(tmp_path, capsys):
    """A scalar where a mapping is expected (e.g. `server: 8554` instead of
    `server: {rtsp_port: 8554}`) used to raise a raw TypeError from
    `set(raw)` in `_construct`, escaping the CLI's error handler entirely:
    a full traceback, no `error:` line, no exit 2."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.replace("server: {rtsp_port: 8554}", "server: 8554"))

    assert main(["serve-config", "-c", str(cfg),
                 "-o", str(tmp_path / "m.yml")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "server" in err


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


def test_module_invocation_runs_main_and_propagates_exit_code(tmp_path):
    """`python -m anycam.cli` must actually run, not silently no-op.

    Without a __main__ guard the module is imported, defines its functions and
    exits 0 regardless of arguments — so a failing config would look like a
    success to any script driving it (the Windows generate.ps1 does exactly
    that).
    """
    import subprocess
    import sys

    cfg = tmp_path / "config.yaml"
    cfg.write_text("cameras: []\n")

    proc = subprocess.run(
        [sys.executable, "-m", "anycam.cli", "serve-config",
         "-c", str(cfg), "-o", str(tmp_path / "m.yml")],
        capture_output=True, text=True)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "at least one camera" in proc.stderr
