from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config
from .host import HostDiscoveryError, discover_windows_host
from .mediamtx import write_mediamtx_config


def _serve_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    # Create parent directory if it doesn't exist, so write_mediamtx_config
    # doesn't raise FileNotFoundError if the output directory is not present
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    out = write_mediamtx_config(config, out_path,
                                hw_mjpeg_decode=not args.no_hw_mjpeg)
    print(f"wrote {out} ({len(config.cameras)} camera(s))")
    print(f"run on Windows:  mediamtx.exe {out}")
    return 0


def _watch(args: argparse.Namespace) -> int:
    from .client import MultiCameraClient

    config = load_config(args.config)
    host = args.host or discover_windows_host()
    print(f"connecting to {host}:{config.server.rtsp_port}")

    with MultiCameraClient(config, host) as client:
        try:
            while True:
                time.sleep(args.interval)
                print(f"--- {time.strftime('%H:%M:%S')} ---")
                for stats in client.stats().values():
                    print(stats.format_line(config.client.watchdog_timeout_s))
        except KeyboardInterrupt:
            return 0


def _devices(args: argparse.Namespace) -> int:
    """List DirectShow devices.

    ffmpeg prints the list to stderr, and depending on the build may exit
    zero or non-zero. Neither is a failure and neither is reported as one --
    measured: ffmpeg 7.x exits 0 here, older builds exit 1.
    """
    proc = subprocess.run(
        [args.ffmpeg, "-hide_banner", "-list_devices", "true",
         "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True)
    sys.stdout.write(proc.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anycam2rtsp")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve-config", help="generate mediamtx.yml (Windows)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-o", "--output", default="mediamtx.yml")
    p.add_argument("--no-hw-mjpeg", action="store_true",
                   help="fall back to CPU MJPEG decode (no mjpeg_cuvid)")
    p.set_defaults(func=_serve_config)

    p = sub.add_parser("watch", help="consume streams in WSL and print stats")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--host", default=None,
                   help="Windows host (default: discover from default route)")
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=_watch)

    p = sub.add_parser("devices", help="list DirectShow capture devices")
    p.add_argument("--ffmpeg", default="ffmpeg.exe")
    p.set_defaults(func=_devices)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        return args.func(args)
    except (ConfigError, HostDiscoveryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m`
    raise SystemExit(main())
