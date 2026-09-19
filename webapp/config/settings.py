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
class DiscSpec:
    """One known source DVD. The registry below is the allow-list of ISOs the
    scanner will ever touch; it also carries the per-disc video profile the
    restoration pipeline needs (broadcast standard + field order), since lsdvd
    reports PAL/NTSC but not TFF/BFF."""

    filename: str            # exact file in source_dir
    slug: str                # stable disc slug (also the title_sources/ prefix)
    collection: str          # wedding/event this disc belongs to (groups multi-disc sets)
    standard: str            # 'pal' | 'ntsc'
    field_order: str         # 'tff' | 'bff'


# Allow-list of source discs. Adding a wedding = add its ISO(s) here and drop the
# file(s) in source/. Field order verified with idet; PAL DVDs are usually bff,
# NTSC usually bff too, but Mo's disc probes as tff — never assume.
DISC_REGISTRY: tuple[DiscSpec, ...] = (
    DiscSpec("weddind_dvd_1.ISO", "dvd1", "Yacoob And Aysha", "pal", "bff"),
    DiscSpec("weddind_dvd_2.ISO", "dvd2", "Yacoob And Aysha", "pal", "bff"),
    DiscSpec("gulfraz_dvd1.iso", "gulfraz1", "Gulfraz And Fahiza", "ntsc", "bff"),
    DiscSpec("gulfraz_dvd2.iso", "gulfraz2", "Gulfraz And Fahiza", "ntsc", "bff"),
    DiscSpec("mo_dvd1.iso", "mo1", "Mo and anisha", "pal", "tff"),
)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    database_path: Path
    source_dir: Path
    disc_specs: tuple[DiscSpec, ...]
    ffmpeg_image: str
    dvdtools_image: str
    host: str
    port: int
    password: str | None
    secret_key: str | None
    free_space_reserve_bytes: int
    auto_start_jobs: bool
    durable_units: bool
    pipeline_path: Path

    @property
    def iso_names(self) -> tuple[str, ...]:
        return tuple(spec.filename for spec in self.disc_specs)

    @property
    def configured_isos(self) -> tuple[Path, ...]:
        return tuple(self.source_dir / spec.filename for spec in self.disc_specs)

    def spec_for(self, filename: str) -> DiscSpec:
        for spec in self.disc_specs:
            if spec.filename == filename:
                return spec
        raise KeyError(f"Unknown source ISO: {filename}")

    def spec_for_slug(self, slug: str) -> DiscSpec:
        for spec in self.disc_specs:
            if spec.slug == slug:
                return spec
        raise KeyError(f"Unknown disc slug: {slug}")


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
    source_dir = PROJECT_ROOT / "source"
    # Only expose discs whose ISO is actually staged locally, so a not-yet-copied
    # disc (e.g. a second disc still on the NAS) never fails a scan-all.
    disc_specs = tuple(spec for spec in DISC_REGISTRY if (source_dir / spec.filename).is_file())
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        database_path=data_dir / "catalog.sqlite",
        source_dir=source_dir,
        disc_specs=disc_specs,
        ffmpeg_image=os.environ.get("WEBAPP_FFMPEG_IMAGE", "linuxserver/ffmpeg:latest"),
        dvdtools_image=os.environ.get("WEBAPP_DVDTOOLS_IMAGE", "wedding-dvdtools:latest"),
        host="127.0.0.1",
        port=8093,
        password=password,
        secret_key=os.environ.get("WEBAPP_SECRET_KEY"),
        free_space_reserve_bytes=int(reserve_gib * 1024**3),
        auto_start_jobs=_bool_env("WEBAPP_AUTO_START_JOBS", False),
        durable_units=_bool_env("WEDDING_DURABLE_UNITS", False),
        pipeline_path=PROJECT_ROOT / "pipeline_v3.sh",
    )
