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
