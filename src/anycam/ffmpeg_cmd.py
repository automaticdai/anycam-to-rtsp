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
        if s.device is None:
            raise ValueError("dshow source requires a device name; got None")
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
    """Render argv as a command line for MediaMTX's runOnInit (Windows shell).

    Quotes arguments containing spaces. Escapes embedded double quotes by
    doubling them. Raises ValueError if an argument contains % (expanded by
    cmd.exe even in quotes) or ASCII control characters.
    """
    result = []
    for arg in argv:
        # Check for problematic characters
        if "%" in arg:
            raise ValueError(
                f"Cannot safely quote argument {arg!r}: contains %, which cmd.exe "
                "expands even inside quotes. Rename the device in Windows Device "
                "Manager or use a different camera."
            )
        for char in arg:
            if ord(char) < 32:  # ASCII control characters
                raise ValueError(
                    f"Cannot safely quote argument {arg!r}: contains control "
                    f"character {char!r}. Rename the device in Windows or use a "
                    "different camera."
                )

        # Quote if it contains spaces, escape any embedded quotes by doubling
        if " " in arg:
            escaped = arg.replace('"', '""')
            result.append(f'"{escaped}"')
        else:
            result.append(arg)
    return " ".join(result)
