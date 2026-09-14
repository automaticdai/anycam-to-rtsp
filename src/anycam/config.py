from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_SOURCE_TYPES = {"dshow", "lavfi"}


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    type: str
    device: str | None = None


@dataclass(frozen=True, slots=True)
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30


@dataclass(frozen=True, slots=True)
class EncodeConfig:
    codec: str = "h264_nvenc"
    preset: str = "p1"
    tune: str = "ull"
    bitrate: str = "8M"
    gop: int = 30


@dataclass(frozen=True, slots=True)
class BackoffConfig:
    initial_s: float = 0.2
    factor: float = 2.0
    max_s: float = 5.0


@dataclass(frozen=True, slots=True)
class ClientConfig:
    watchdog_timeout_s: float = 2.0
    backoff: BackoffConfig = field(default_factory=BackoffConfig)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    rtsp_port: int = 8554


@dataclass(frozen=True, slots=True)
class CameraConfig:
    id: str
    source: SourceConfig
    video: VideoConfig
    encode: EncodeConfig


@dataclass(frozen=True, slots=True)
class AppConfig:
    server: ServerConfig
    cameras: tuple[CameraConfig, ...]
    client: ClientConfig


def _camera(raw: dict) -> CameraConfig:
    if "id" not in raw:
        raise ConfigError("camera entry is missing 'id'")
    src = raw.get("source") or {}
    stype = src.get("type")
    if stype not in VALID_SOURCE_TYPES:
        raise ConfigError(
            f"camera {raw['id']}: unknown source type {stype!r}; "
            f"expected one of {sorted(VALID_SOURCE_TYPES)}")
    if stype == "dshow" and not src.get("device"):
        raise ConfigError(f"camera {raw['id']}: dshow source requires 'device'")
    return CameraConfig(
        id=str(raw["id"]),
        source=SourceConfig(type=stype, device=src.get("device")),
        video=VideoConfig(**(raw.get("video") or {})),
        encode=EncodeConfig(**(raw.get("encode") or {})),
    )


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cams_raw = raw.get("cameras") or []
    if not cams_raw:
        raise ConfigError("configuration must define at least one camera")

    cameras = tuple(_camera(c) for c in cams_raw)
    ids = [c.id for c in cameras]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate camera ids: {sorted(dupes)}")

    client_raw = dict(raw.get("client") or {})
    backoff = BackoffConfig(**(client_raw.pop("backoff", None) or {}))
    return AppConfig(
        server=ServerConfig(**(raw.get("server") or {})),
        cameras=cameras,
        client=ClientConfig(backoff=backoff, **client_raw),
    )
