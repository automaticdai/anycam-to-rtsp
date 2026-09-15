from __future__ import annotations

import subprocess


class HostDiscoveryError(RuntimeError):
    """Raised when the Windows host address cannot be determined."""


def parse_default_gateway(route_output: str) -> str:
    """Extract the default gateway address from `ip route show default`.

    Under WSL2 NAT networking the default gateway is the Windows host. The
    address changes across restarts, so it must never be hardcoded.
    """
    for line in route_output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "default" and fields[1] == "via":
            return fields[2]
    raise HostDiscoveryError(
        f"no default route found in: {route_output!r}")


def discover_windows_host() -> str:
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, check=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostDiscoveryError(f"could not run 'ip route': {exc}") from exc
    return parse_default_gateway(out)
