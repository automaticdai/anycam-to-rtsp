"""Print `id<TAB>type<TAB>device` for each camera in a config file.

A helper for the PowerShell scripts: parsing YAML in PowerShell is avoidable
work, and passing Python source inline via `-c` runs into PowerShell mangling
embedded quotes. A file on disk sidesteps both.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from anycam.config import ConfigError, load_config  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: list_cameras.py <config.yaml>", file=sys.stderr)
        return 2
    try:
        config = load_config(sys.argv[1])
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for cam in config.cameras:
        print(f"{cam.id}\t{cam.source.type}\t{cam.source.device or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
