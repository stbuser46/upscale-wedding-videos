from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from webapp.config import Settings, load_settings
from webapp.db import connect, migrate, transaction, utc_now


PRIORITIES = {"high", "normal", "low", "skip"}


class ScanError(RuntimeError):
    pass


def _run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _iso_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with path.open("rb") as source:
        digest.update(source.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            source.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(source.read(1024 * 1024))
    return f"sha256-sampled:{digest.hexdigest()}"


def _docker_lsdvd(settings: Settings, iso: Path) -> tuple[str, str]:
    if iso.parent != settings.source_dir or iso.name not in settings.iso_names:
        raise ScanError("Refusing to scan an ISO outside the configured source catalog")
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{settings.source_dir}:/source:ro",
            settings.dvdtools_image,
            "-x",
            "-Ox",
            f"/source/{iso.name}",
        ]
    )
    xml_start = result.stdout.find("<?xml")
    if xml_start < 0:
        raise ScanError("lsdvd did not return XML discovery output")
    stdout_preamble = result.stdout[:xml_start].strip()
    audit = "\n".join(part for part in (stdout_preamble, result.stderr.strip()) if part)
    return result.stdout[xml_start:], audit


def _text(element: ET.Element, name: str, default: str = "") -> str:
    child = element.find(name)
    return child.text.strip() if child is not None and child.text else default


def _stream_dict(element: ET.Element) -> dict[str, str | int | float]:
    result: dict[str, str | int | float] = {}
    for child in element:
        value: str | int | float = (child.text or "").strip()
        if value.isdigit():
            value = int(value)
        else:
            try:
                value = float(value)
            except ValueError:
                pass
        result[child.tag] = value
    return result


def _parse_catalog(xml_text: str) -> dict[str, object]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ScanError(f"lsdvd returned invalid XML: {exc}") from exc
    titles: list[dict[str, object]] = []
    for track in root.findall("track"):
        duration_ms = round(float(_text(track, "length", "0")) * 1000)
        chapters: list[dict[str, int]] = []
        cursor_ms = 0
        for chapter in track.findall("chapter"):
            chapter_duration = round(float(_text(chapter, "length", "0")) * 1000)
            if chapter_duration <= 0:
                continue
            chapters.append(
                {
                    "number": int(_text(chapter, "ix", str(len(chapters) + 1))),
                    "start_ms": cursor_ms,
                    "end_ms": cursor_ms + chapter_duration,
                    "duration_ms": chapter_duration,
                }
            )
            cursor_ms += chapter_duration
        if chapters:
            chapters[-1]["end_ms"] = duration_ms
            chapters[-1]["duration_ms"] = max(1, duration_ms - chapters[-1]["start_ms"])
        video = {
            key: _text(track, key)
            for key in ("fps", "format", "aspect", "width", "height", "df", "vts", "ttn")
            if _text(track, key)
        }
        titles.append(
            {
                "number": int(_text(track, "ix", "0")),
                "duration_ms": duration_ms,
                "angles": int(_text(track, "angles", "1")),
                "video": video,
                "audio": [_stream_dict(node) for node in track.findall("audio")],
                "subtitles": [_stream_dict(node) for node in track.findall("subp")],
                "chapters": chapters,
                "raw": _stream_dict(track),
            }
        )
    if not titles:
        raise ScanError("lsdvd found no DVD titles")
    return {
        "label": _text(root, "title", "Unlabelled DVD"),
        "provider_id": _text(root, "provider_id"),
        "vmg_id": _text(root, "vmg_id"),
        "longest_track": int(_text(root, "longest_track", "0")),
        "titles": titles,
    }


def scan_disc(settings: Settings, iso: Path) -> int:
    if not iso.is_file():
        raise ScanError(f"Configured ISO is missing: {iso}")
    migrate(settings.database_path)
    now = utc_now()
    slug = f"dvd{settings.iso_names.index(iso.name) + 1}"
    fingerprint = _iso_fingerprint(iso)
    with connect(settings.database_path) as db, transaction(db):
        db.execute(
            """INSERT INTO discs
               (slug, source_filename, source_path, size_bytes, fingerprint,
                scan_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'scanning', ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 source_filename=excluded.source_filename,
                 source_path=excluded.source_path,
                 size_bytes=excluded.size_bytes,
                 fingerprint=excluded.fingerprint,
                 scan_status='scanning', scan_error=NULL, updated_at=excluded.updated_at""",
            (slug, iso.name, str(iso), iso.stat().st_size, fingerprint, now, now),
        )
        disc_id = db.execute("SELECT id FROM discs WHERE slug = ?", (slug,)).fetchone()["id"]
    try:
        xml_text, stderr_text = _docker_lsdvd(settings, iso)
        parsed = _parse_catalog(xml_text)
        scan_dir = settings.data_dir / "scans" / slug
        scan_dir.mkdir(parents=True, exist_ok=True)
        xml_path = scan_dir / "lsdvd.xml"
        xml_partial = xml_path.with_suffix(".partial.xml")
        xml_partial.write_text(xml_text, encoding="utf-8")
        os.replace(xml_partial, xml_path)
        audit_path = scan_dir / "lsdvd.stderr.log"
        audit_partial = audit_path.with_suffix(".partial.log")
        audit_partial.write_text(stderr_text, encoding="utf-8")
        os.replace(audit_partial, audit_path)
        durations = [int(item["duration_ms"]) for item in parsed["titles"]]
        with connect(settings.database_path) as db, transaction(db):
            for title in parsed["titles"]:
                number = int(title["number"])
                duration_ms = int(title["duration_ms"])
                duplicate = durations.count(duration_ms) > 1
                db.execute(
                    """INSERT INTO titles
                       (disc_id, title_number, duration_ms, angles, video_json,
                        audio_json, subtitles_json, raw_navigation_json,
                        likely_menu, likely_duplicate, likely_short,
                        source_cache_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(disc_id, title_number) DO UPDATE SET
                         duration_ms=excluded.duration_ms, angles=excluded.angles,
                         video_json=excluded.video_json, audio_json=excluded.audio_json,
                         subtitles_json=excluded.subtitles_json,
                         raw_navigation_json=excluded.raw_navigation_json,
                         likely_menu=excluded.likely_menu,
                         likely_duplicate=excluded.likely_duplicate,
                         likely_short=excluded.likely_short,
                         source_cache_path=COALESCE(excluded.source_cache_path, titles.source_cache_path),
                         updated_at=excluded.updated_at""",
                    (
                        disc_id,
                        number,
                        duration_ms,
                        int(title["angles"]),
                        json.dumps(title["video"], sort_keys=True),
                        json.dumps(title["audio"], sort_keys=True),
                        json.dumps(title["subtitles"], sort_keys=True),
                        json.dumps(title["raw"], sort_keys=True),
                        int(duration_ms < 60_000),
                        int(duplicate),
                        int(duration_ms < 120_000),
                        None,
                        now,
                        now,
                    ),
                )
                title_id = db.execute(
                    "SELECT id FROM titles WHERE disc_id = ? AND title_number = ?",
                    (disc_id, number),
                ).fetchone()["id"]
                for chapter in title["chapters"]:
                    chapter_number = int(chapter["number"])
                    db.execute(
                        """INSERT INTO chapters
                           (title_id, chapter_number, start_ms, end_ms, duration_ms,
                            generated_label, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(title_id, chapter_number) DO UPDATE SET
                             start_ms=excluded.start_ms, end_ms=excluded.end_ms,
                             duration_ms=excluded.duration_ms,
                             generated_label=excluded.generated_label,
                             updated_at=excluded.updated_at""",
                        (
                            title_id,
                            chapter_number,
                            int(chapter["start_ms"]),
                            int(chapter["end_ms"]),
                            int(chapter["duration_ms"]),
                            f"Chapter {chapter_number:02d}",
                            now,
                            now,
                        ),
                    )
            db.execute(
                """UPDATE discs SET label=?, scan_status='complete', scan_error=NULL,
                   raw_scan_path=?, scanned_at=?, updated_at=? WHERE id=?""",
                (parsed["label"], str(xml_path.relative_to(settings.data_dir)), now, now, disc_id),
            )
    except Exception as exc:
        with connect(settings.database_path) as db:
            db.execute(
                "UPDATE discs SET scan_status='failed', scan_error=?, updated_at=? WHERE id=?",
                (str(exc), utc_now(), disc_id),
            )
        raise
    return disc_id


def scan_all_discs(settings: Settings | None = None) -> list[int]:
    settings = settings or load_settings()
    return [scan_disc(settings, iso) for iso in settings.configured_isos]


def _ensure_title_source(settings: Settings, title_id: int) -> Path:
    with connect(settings.database_path) as db:
        row = db.execute(
            """SELECT t.*, d.slug, d.source_path,
                      (SELECT COUNT(*) FROM titles tx WHERE tx.disc_id=t.disc_id) AS title_count
               FROM titles t JOIN discs d ON d.id=t.disc_id WHERE t.id=?""",
            (title_id,),
        ).fetchone()
    if row is None:
        raise ScanError("Unknown title")
    if row["source_cache_path"]:
        path = Path(row["source_cache_path"])
        if path.is_file() and path.is_relative_to(settings.data_dir):
            return path
    source_iso = Path(row["source_path"])
    if source_iso.parent != settings.source_dir:
        raise ScanError("Catalog source escaped the configured source directory")
    output_dir = settings.data_dir / "title_sources"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{row['slug']}_title{row['title_number']:02d}.vob"
    partial = output.with_suffix(".partial.vob")
    partial.unlink(missing_ok=True)
    _run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{settings.source_dir}:/source:ro",
            "-v", f"{output_dir}:/data:rw",
            "--entrypoint", "mplayer", settings.dvdtools_image,
            "-really-quiet", "-nolirc", "-dvd-device", f"/source/{source_iso.name}",
            f"dvd://{row['title_number']}", "-dumpstream", "-dumpfile", f"/data/{partial.name}",
        ],
        capture=False,
    )
    if not partial.is_file() or partial.stat().st_size == 0:
        raise ScanError("DVD title extraction produced no data")
    media = _ffprobe(settings, partial)
    actual_ms = round(float(media.get("format", {}).get("duration", 0)) * 1000)
    if abs(actual_ms - row["duration_ms"]) > 2_000:
        partial.unlink(missing_ok=True)
        raise ScanError(
            f"DVD title extraction duration mismatch: expected {row['duration_ms']} ms, "
            f"found {actual_ms} ms"
        )
    os.replace(partial, output)
    with connect(settings.database_path) as db:
        db.execute(
            "UPDATE titles SET source_cache_path=?, updated_at=? WHERE id=?",
            (str(output), utc_now(), title_id),
        )
    return output


def _container_media_path(settings: Settings, path: Path) -> tuple[list[str], str]:
    path = path.resolve()
    if path.is_relative_to(settings.source_dir):
        return ["-v", f"{settings.source_dir}:/source:ro"], f"/source/{path.name}"
    if path.is_relative_to(settings.data_dir):
        relative = path.relative_to(settings.data_dir)
        return ["-v", f"{settings.data_dir}:/data:rw"], f"/data/{relative}"
    raise ScanError("Media path is outside configured source and data directories")


def _ffprobe(settings: Settings, path: Path) -> dict[str, object]:
    mounts, container_path = _container_media_path(settings, path)
    result = _run(
        [
            "docker", "run", "--rm", "--network", "none", *mounts,
            "--entrypoint", "ffprobe", settings.ffmpeg_image,
            "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", container_path,
        ]
    )
    return json.loads(result.stdout)


def _register_artifact(
    settings: Settings,
    *,
    title_id: int,
    chapter_id: int | None,
    kind: str,
    path: Path,
    mime_type: str,
    duration_ms: int | None,
    media: dict[str, object],
) -> None:
    relative = str(path.relative_to(settings.data_dir))
    now = utc_now()
    with connect(settings.database_path) as db:
        db.execute(
            """INSERT INTO artifacts
               (title_id, chapter_id, kind, relative_path, mime_type, size_bytes,
                duration_ms, validation_state, media_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?, ?)
               ON CONFLICT(relative_path) DO UPDATE SET
                 size_bytes=excluded.size_bytes, duration_ms=excluded.duration_ms,
                 validation_state='valid', media_json=excluded.media_json,
                 updated_at=excluded.updated_at""",
            (
                title_id, chapter_id, kind, relative, mime_type, path.stat().st_size,
                duration_ms, json.dumps(media, sort_keys=True), now, now,
            ),
        )


def _generate_chapter_media(settings: Settings, row: object, *, force: bool) -> None:
    source = _ensure_title_source(settings, row["title_id"])
    output_dir = settings.data_dir / "review" / row["disc_slug"] / f"title{row['title_number']:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    proxy = output_dir / f"chapter{row['chapter_number']:02d}.mp4"
    thumbnail = output_dir / f"chapter{row['chapter_number']:02d}.jpg"
    start = row["start_ms"] / 1000
    duration = row["duration_ms"] / 1000
    source_mounts, container_source = _container_media_path(settings, source)
    data_mount = ["-v", f"{settings.data_dir}:/data:rw"]
    proxy_partial = proxy.with_suffix(".partial.mp4")
    thumb_partial = thumbnail.with_suffix(".partial.jpg")
    with connect(settings.database_path) as db:
        db.execute(
            "UPDATE chapters SET proxy_state='generating', proxy_error=NULL, updated_at=? WHERE id=?",
            (utc_now(), row["chapter_id"]),
        )
    try:
        if force or not proxy.is_file():
            proxy_partial.unlink(missing_ok=True)
            _run(
                [
                    "docker", "run", "--rm", "--network", "none", *source_mounts, *data_mount,
                    "--entrypoint", "ffmpeg", settings.ffmpeg_image,
                    "-hide_banner", "-loglevel", "warning", "-y",
                    "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", container_source,
                    "-map", "0:v:0", "-map", "0:a:0?", "-vf",
                    "bwdif=mode=send_frame:parity=bff,scale=640:480:flags=lanczos,setsar=1",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                    f"/data/{proxy_partial.relative_to(settings.data_dir)}",
                ],
                capture=False,
            )
            os.replace(proxy_partial, proxy)
        if force or not thumbnail.is_file():
            thumb_partial.unlink(missing_ok=True)
            thumb_at = start + min(30.0, max(0.5, duration / 3))
            _run(
                [
                    "docker", "run", "--rm", "--network", "none", *source_mounts, *data_mount,
                    "--entrypoint", "ffmpeg", settings.ffmpeg_image,
                    "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{thumb_at:.3f}",
                    "-i", container_source, "-map", "0:v:0", "-frames:v", "1",
                    "-vf", "bwdif=mode=send_frame:parity=bff,scale=640:480:flags=lanczos,setsar=1",
                    f"/data/{thumb_partial.relative_to(settings.data_dir)}",
                ]
            )
            os.replace(thumb_partial, thumbnail)
        media = _ffprobe(settings, proxy)
        video = next((s for s in media.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video or video.get("codec_name") != "h264":
            raise ScanError("Proxy validation failed: H.264 video stream missing")
        _register_artifact(
            settings, title_id=row["title_id"], chapter_id=row["chapter_id"],
            kind="chapter_proxy", path=proxy, mime_type="video/mp4",
            duration_ms=row["duration_ms"], media=media,
        )
        _register_artifact(
            settings, title_id=row["title_id"], chapter_id=row["chapter_id"],
            kind="thumbnail", path=thumbnail, mime_type="image/jpeg",
            duration_ms=None, media={},
        )
        with connect(settings.database_path) as db:
            db.execute(
                "UPDATE chapters SET proxy_state='ready', proxy_error=NULL, updated_at=? WHERE id=?",
                (utc_now(), row["chapter_id"]),
            )
    except Exception as exc:
        proxy_partial.unlink(missing_ok=True)
        thumb_partial.unlink(missing_ok=True)
        with connect(settings.database_path) as db:
            db.execute(
                "UPDATE chapters SET proxy_state='failed', proxy_error=?, updated_at=? WHERE id=?",
                (str(exc), utc_now(), row["chapter_id"]),
            )
        raise


def generate_review_media(
    settings: Settings | None = None,
    *,
    disc_slug: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> int:
    settings = settings or load_settings()
    migrate(settings.database_path)
    params: list[object] = []
    where = "WHERE c.priority != 'skip' AND t.likely_menu=0"
    if disc_slug:
        if disc_slug not in {"dvd1", "dvd2"}:
            raise ScanError("Disc must be dvd1 or dvd2")
        where += " AND d.slug=?"
        params.append(disc_slug)
    query = f"""SELECT c.id AS chapter_id, c.title_id, c.chapter_number,
                       c.start_ms, c.duration_ms, t.title_number, d.slug AS disc_slug
                FROM chapters c
                JOIN titles t ON t.id=c.title_id JOIN discs d ON d.id=t.disc_id
                {where} ORDER BY d.id, t.title_number, c.chapter_number"""
    with connect(settings.database_path) as db:
        rows = list(db.execute(query, params))
    if limit is not None:
        rows = rows[: max(0, limit)]
    for row in rows:
        print(f"Generating {row['disc_slug']} title {row['title_number']} chapter {row['chapter_number']}…")
        _generate_chapter_media(settings, row, force=force)
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scan fixed wedding DVD ISOs and create review media")
    parser.add_argument("--scan", action="store_true", help="scan both configured ISOs")
    parser.add_argument("--proxies", action="store_true", help="generate chapter proxies and thumbnails")
    parser.add_argument("--disc", choices=("dvd1", "dvd2"), help="limit proxy generation")
    parser.add_argument("--limit", type=int, help="limit proxy count for a verification run")
    parser.add_argument("--force", action="store_true", help="regenerate existing review media")
    args = parser.parse_args(argv)
    if not args.scan and not args.proxies:
        parser.error("choose --scan and/or --proxies")
    settings = load_settings()
    if args.scan:
        ids = scan_all_discs(settings)
        print(f"Scanned {len(ids)} discs")
    if args.proxies:
        count = generate_review_media(settings, disc_slug=args.disc, limit=args.limit, force=args.force)
        print(f"Generated or validated {count} chapter proxy set(s)")


if __name__ == "__main__":
    main()
