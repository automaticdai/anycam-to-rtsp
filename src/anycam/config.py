from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import yaml

VALID_SOURCE_TYPES = {"dshow", "lavfi"}

# Conservative charset for a camera id: it flows unchecked into a MediaMTX
# path key, a publish URL and a consume URL, so anything that could upset
# any of those three (spaces, slashes, ...) must be rejected up front rather
# than fail in three different places at once.
_VALID_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or inconsistent."""


_T = TypeVar("_T")


def _construct(cls: type[_T], raw: dict, section: str, **fixed) -> _T:
    """Build a config dataclass from a raw mapping, turning an unknown or
    otherwise invalid key into a `ConfigError` that names the offending key
    and section -- instead of a raw `TypeError` with no such context, which
    previously escaped all the way out of `load_config` uncaught.

    `fixed` are additional, already-validated constructor kwargs (e.g. a
    pre-built `BackoffConfig`) that are not expected to appear in `raw`.
    """
    if not isinstance(raw, dict):
        # A scalar where a mapping is expected (e.g. `server: 8554` instead
        # of `server: {rtsp_port: 8554}`) used to reach `set(raw)` below
        # with `raw` being that scalar, raising a raw `TypeError` ("'int'
        # object is not iterable") with no section context -- outside this
        # function's own try/except, so it escaped `load_config` uncaught.
        raise ConfigError(
            f"{section!r} section must be a mapping of settings; found "
            f"{type(raw).__name__} ({raw!r}) instead")
    valid = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - valid
    if unknown:
        raise ConfigError(
            f"unknown key(s) {sorted(unknown)} in {section!r} section; "
            f"expected one of {sorted(valid - set(fixed))}")
    try:
        return cls(**raw, **fixed)
    except TypeError as exc:
        raise ConfigError(f"invalid {section!r} section: {exc}") from exc


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
    cam_id = raw["id"]
    if not _VALID_CAMERA_ID.match(str(cam_id)):
        raise ConfigError(
            f"camera id {cam_id!r} is not valid: it must contain only "
            f"letters, digits, underscores, and hyphens (it becomes a "
            f"MediaMTX path key and part of a publish/consume URL)")
    src = raw.get("source") or {}
    stype = src.get("type")
    if stype not in VALID_SOURCE_TYPES:
        raise ConfigError(
            f"camera {cam_id}: unknown source type {stype!r}; "
            f"expected one of {sorted(VALID_SOURCE_TYPES)}")
    if stype == "dshow" and not src.get("device"):
        raise ConfigError(f"camera {cam_id}: dshow source requires 'device'")
    return CameraConfig(
        id=str(cam_id),
        source=SourceConfig(type=stype, device=src.get("device")),
        video=_construct(VideoConfig, raw.get("video") or {},
                         f"camera {cam_id} video"),
        encode=_construct(EncodeConfig, raw.get("encode") or {},
                          f"camera {cam_id} encode"),
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
    backoff = _construct(BackoffConfig, client_raw.pop("backoff", None) or {},
                        "client.backoff")
    return AppConfig(
        server=_construct(ServerConfig, raw.get("server") or {}, "server"),
        cameras=cameras,
        client=_construct(ClientConfig, client_raw, "client", backoff=backoff),
    )
