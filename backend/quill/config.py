"""Runtime settings, read once from QUILL_* environment variables.

    from quill.config import Settings
    s = Settings.from_env()

Settings is frozen. Admin-editable values (stt_model, text_model, vision_model,
video_retention_days) can be overridden at runtime through the `settings` table;
use `Settings.with_overrides(...)` / `quill.db.effective_settings(...)` to apply them.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

GiB = 1024 ** 3

_DEFAULT_MODEL = "google/gemma-4-12B-it-qat-w4a16-ct"


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get("QUILL_" + key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"QUILL_{key} must be an integer, got {raw!r}") from exc


def _str(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get("QUILL_" + key, "").strip()
    return raw or default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/var/lib/quill")
    gateway_url: str = "http://localhost:4000/v1"
    stt_model: str = "aer-stt-v1"
    vision_model: str = _DEFAULT_MODEL
    text_model: str = _DEFAULT_MODEL
    diarizer_cmd: str = "/opt/quill/diarizer-venv/bin/quill-diarize"
    max_upload_bytes: int = 10 * GiB
    # Days after the pipeline finishes before the source video is deleted.
    # 0 deletes it right after processing; a negative value keeps it forever.
    video_retention_days: int = 14
    stt_concurrency: int = 3
    vision_concurrency: int = 2
    frames_per_hour: int = 40
    max_frames: int = 200
    secret_key: str = ""
    public_url: str = ""
    # Additions (not in the contract list, all optional):
    disk_reserve_bytes: int = 2 * GiB          # QUILL_DISK_RESERVE_BYTES
    frontend_dist: Path | None = None          # QUILL_FRONTEND_DIST (default: ../frontend/dist)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        dist = env.get("QUILL_FRONTEND_DIST", "").strip()
        return cls(
            data_dir=Path(_str(env, "DATA_DIR", "/var/lib/quill")),
            gateway_url=_str(env, "GATEWAY_URL", "http://localhost:4000/v1").rstrip("/"),
            stt_model=_str(env, "STT_MODEL", "aer-stt-v1"),
            vision_model=_str(env, "VISION_MODEL", _DEFAULT_MODEL),
            text_model=_str(env, "TEXT_MODEL", _DEFAULT_MODEL),
            diarizer_cmd=_str(env, "DIARIZER_CMD", "/opt/quill/diarizer-venv/bin/quill-diarize"),
            max_upload_bytes=_int(env, "MAX_UPLOAD_BYTES", 10 * GiB),
            video_retention_days=_int(env, "VIDEO_RETENTION_DAYS", 14),
            stt_concurrency=max(1, _int(env, "STT_CONCURRENCY", 3)),
            vision_concurrency=max(1, _int(env, "VISION_CONCURRENCY", 2)),
            frames_per_hour=max(0, _int(env, "FRAMES_PER_HOUR", 40)),
            max_frames=max(0, _int(env, "MAX_FRAMES", 200)),
            secret_key=env.get("QUILL_SECRET_KEY", ""),
            public_url=_str(env, "PUBLIC_URL", "").rstrip("/"),
            disk_reserve_bytes=max(0, _int(env, "DISK_RESERVE_BYTES", 2 * GiB)),
            frontend_dist=Path(dist) if dist else None,
        )

    # Derived paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "quill.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def media_root(self) -> Path:
        return self.data_dir / "media"

    def media_dir(self, meeting_id: str) -> Path:
        return self.media_root / meeting_id

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.lower().startswith("https://")

    @property
    def frontend_dist_dir(self) -> Path:
        if self.frontend_dist is not None:
            return self.frontend_dist
        return Path(__file__).resolve().parents[2] / "frontend" / "dist"

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.uploads_dir, self.media_root):
            path.mkdir(parents=True, exist_ok=True)

    # Runtime overrides ---------------------------------------------------
    OVERRIDABLE = ("stt_model", "text_model", "vision_model", "video_retention_days")

    def with_overrides(self, overrides: Mapping[str, Any]) -> "Settings":
        clean: dict[str, Any] = {}
        for key, value in overrides.items():
            if key not in self.OVERRIDABLE or value is None:
                continue
            if key == "video_retention_days":
                try:
                    clean[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(value, str) and value.strip():
                clean[key] = value.strip()
        return dataclasses.replace(self, **clean) if clean else self
