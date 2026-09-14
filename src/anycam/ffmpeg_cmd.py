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
        # -re paces generation to wallclock rate. Without it lavfi generates
        # testsrc2 as fast as the encoder can run (measured: 308.7fps
        # against a configured 30), which is only correct for a synthetic
        # source used in tests -- dshow must never get this flag, since
        # real hardware already paces the stream itself.
        argv += ["-re", "-f", "lavfi",
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
    """Render argv as a command line for MediaMTX's runOnInit.

    MediaMTX parses the output via go-shellquote.Split() and execs the result
    directly without a shell (verified against v1.9.3; only uses cmd.exe if
    the command literally starts with "cmd " or "cmd.exe ").

    Quotes arguments containing spaces. Escapes backslashes and double quotes
    using backslash escaping (go-shellquote's recognized escape sequence).
    Raises ValueError if an argument contains ASCII control characters
    (which break go-shellquote's parser).
    """
    result = []
    for arg in argv:
        # Check for control characters that break go-shellquote's Split()
        for char in arg:
            if ord(char) < 32:  # ASCII control characters (including newline)
                raise ValueError(
                    f"Cannot quote argument {arg!r}: contains control character "
                    f"{char!r}. Use a different argument or value."
                )

        # Escape backslashes first, then quotes (go-shellquote's escape rules)
        escaped = arg.replace('\\', '\\\\').replace('"', '\\"')

        # Quote if it contains spaces, special characters, or an apostrophe.
        # go-shellquote treats a bare `'` as a quote character in an
        # unquoted word, so an unquoted apostrophe is silently mis-parsed
        # (confirmed against real MediaMTX v1.9.3: the path never started
        # and nothing matching "error" was logged, even at logLevel: debug).
        if " " in arg or "'" in arg or escaped != arg:
            result.append(f'"{escaped}"')
        else:
            result.append(arg)
    return " ".join(result)
