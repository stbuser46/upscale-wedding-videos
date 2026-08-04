from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "webapp" / "data"


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    database_path: Path
    source_dir: Path
    iso_names: tuple[str, ...]
    ffmpeg_image: str
    dvdtools_image: str
    host: str
    port: int
    password: str | None
    secret_key: str | None
    free_space_reserve_bytes: int
    auto_start_jobs: bool
    pipeline_path: Path

    @property
    def configured_isos(self) -> tuple[Path, ...]:
        return tuple(self.source_dir / name for name in self.iso_names)


def load_settings(*, require_password: bool = False) -> Settings:
    data_dir = Path(os.environ.get("WEBAPP_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
    if not data_dir.is_relative_to(PROJECT_ROOT):
        raise RuntimeError("WEBAPP_DATA_DIR must be inside the project for fixed Docker mounts")
    password = os.environ.get("WEBAPP_PASSWORD")
    if require_password and not password:
        raise RuntimeError("WEBAPP_PASSWORD must be set before starting the web server")
    reserve_gib = float(os.environ.get("WEBAPP_FREE_SPACE_RESERVE_GIB", "100"))
    if reserve_gib < 0:
        raise RuntimeError("WEBAPP_FREE_SPACE_RESERVE_GIB cannot be negative")
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        database_path=data_dir / "catalog.sqlite",
        source_dir=PROJECT_ROOT / "source",
        iso_names=("weddind_dvd_1.ISO", "weddind_dvd_2.ISO"),
        ffmpeg_image=os.environ.get("WEBAPP_FFMPEG_IMAGE", "linuxserver/ffmpeg:latest"),
        dvdtools_image=os.environ.get("WEBAPP_DVDTOOLS_IMAGE", "wedding-dvdtools:latest"),
        host="127.0.0.1",
        port=8093,
        password=password,
        secret_key=os.environ.get("WEBAPP_SECRET_KEY"),
        free_space_reserve_bytes=int(reserve_gib * 1024**3),
        auto_start_jobs=_bool_env("WEBAPP_AUTO_START_JOBS", False),
        pipeline_path=PROJECT_ROOT / "pipeline_v3.sh",
    )
