"""Measure true glass-to-inference latency.

Procedure:
  1. Run `python tools/calibrate_latency.py display` IN WSL, in a terminal
     window you can see on the Windows screen. That window is displayed by
     Windows, so the camera can film it -- and because the clock and the
     measurement then share one clock, no Windows/WSL skew enters the result.
     Running the clock on Windows instead reintroduces exactly the cross-OS
     skew this measurement exists to avoid.
  2. Point one camera at that terminal window, filling as much of the frame
     as you can and focused on the digits.
  3. Run `python tools/calibrate_latency.py measure -c config.yaml --camera cam0`
     in WSL. It saves annotated frames showing the displayed time alongside
     the WSL arrival time.
  4. Read the digits off the saved frames; the difference is the total
     pipeline latency, including exposure and USB transfer.

Reading digits is deliberately manual. Automating OCR would add a dependency
and a failure mode to a procedure run a handful of times per hardware change.

`measure` needs OpenCV to burn the arrival timestamp into the saved frame.
It is not part of the base install (WSL clients that only consume frames
never need it) -- install it with `pip install -e ".[calibration]"` first.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam2rtsp.client import MultiCameraClient          # noqa: E402
from anycam2rtsp.config import load_config                # noqa: E402
from anycam2rtsp.host import discover_windows_host        # noqa: E402


def display() -> int:
    """Print a millisecond clock. Run this on Windows, filmed by the camera."""
    try:
        while True:
            print(f"\r{time.strftime('%H:%M:%S')}."
                  f"{int(time.time() * 1000) % 1000:03d}", end="", flush=True)
            time.sleep(0.001)
    except KeyboardInterrupt:
        print()
        return 0


def measure(args: argparse.Namespace) -> int:
    try:
        import cv2  # only needed for the calibration path
    except ImportError as exc:
        print("error: opencv-python is required for `measure` but is not "
              "installed. Install the calibration extra with:\n"
              "    pip install -e \".[calibration]\"", file=sys.stderr)
        raise SystemExit(2) from exc

    config = load_config(args.config)
    host = args.host or discover_windows_host()
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    with MultiCameraClient(config, host) as client:
        print(f"capturing {args.samples} samples from {args.camera}")
        captured = 0
        while captured < args.samples:
            frame = client.take(args.camera)
            if frame is None:
                time.sleep(0.01)
                continue
            wall = time.strftime("%H:%M:%S") + \
                f".{int(time.time() * 1000) % 1000:03d}"
            image = frame.image.copy()
            cv2.putText(image, f"WSL arrival: {wall}", (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            cv2.imwrite(str(outdir / f"sample_{captured:03d}.png"), image)
            captured += 1
            time.sleep(args.gap)

    print(f"wrote {captured} annotated frames to {outdir}")
    print("Read the on-screen clock in each image and subtract it from the "
          "annotated WSL arrival time. That difference is total pipeline "
          "latency, exposure and USB transfer included.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="calibrate_latency")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("display", help="run the millisecond clock (on Windows)")

    p = sub.add_parser("measure", help="capture annotated frames (in WSL)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--camera", default="cam0")
    p.add_argument("--host", default=None)
    p.add_argument("--samples", type=int, default=20)
    p.add_argument("--gap", type=float, default=0.5)
    p.add_argument("-o", "--output", default="calibration")

    args = parser.parse_args()
    return display() if args.command == "display" else measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
