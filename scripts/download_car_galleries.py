#!/usr/bin/env python3
"""
Download matched OEM gallery images and write a main-site import JSON.

Stores binaries once per source gallery, then maps each car entity_id to
relative file paths. Color swatches are skipped.

Usage:
  python scripts/download_car_galleries.py --limit-galleries 2 --limit-images 3 -v
  python scripts/download_car_galleries.py --resume -v
  docker exec -i seenad_backend python3 scripts/download_car_galleries.py --resume -v

Output:
  data/car_profile_galleries/images/{source}/{gallery_key}/0001.jpg
  data/car_profile_galleries/car_profile_galleries.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import urllib3
from requests.exceptions import RequestException, SSLError, Timeout

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import probe_car_gallery_poc as poc  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGGER = logging.getLogger("car-gallery-download")
DEFAULT_MATCHES = Path("data/car_gallery_entity_matches.json")
DEFAULT_GALLERIES = Path("data/raw_car_galleries.jsonl")
DEFAULT_OUT_DIR = Path("data/car_profile_galleries")
MANIFEST_NAME = "car_profile_galleries.json"
SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9._-]+")
SKIP_KIND = {"color_swatch"}
SKIP_PATH_RE = re.compile(r"/colors?/|/swatch", re.I)
MIN_BYTES = 2048
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def gallery_key(record: dict[str, Any]) -> str:
    source = record.get("source") or "unknown"
    url = record.get("source_url") or ""
    parsed = urlparse(url)
    if source == "ikco":
        qs = parse_qs(parsed.query)
        pid = (qs.get("ID") or qs.get("id") or [""])[0]
        if pid:
            return f"id-{pid}"
    if source == "bahman":
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            return tail
    slug = record.get("slug") or parsed.path.rstrip("/").rsplit("/", 1)[-1]
    key = SAFE_KEY_RE.sub("-", slug or "gallery").strip("-").lower()
    return key or "gallery"


def encode_url(url: str) -> str:
    parsed = urlparse(url)
    path = quote(unquote(parsed.path), safe="/")
    return urlunparse(parsed._replace(path=path))


def sniff_format(data: bytes, content_type: str | None) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and b"WEBP" in data[:16]:
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    ctype = (content_type or "").lower()
    if "jpeg" in ctype or "jpg" in ctype:
        return "jpg"
    if "png" in ctype:
        return "png"
    if "webp" in ctype:
        return "webp"
    if "gif" in ctype:
        return "gif"
    return None


def keep_image(item: dict[str, Any]) -> bool:
    kind = (item.get("kind") or "gallery").lower()
    url = item.get("url") or ""
    if kind in SKIP_KIND:
        return False
    if SKIP_PATH_RE.search(urlparse(url).path):
        return False
    return bool(url)


def relative_file(source: str, key: str, filename: str) -> str:
    return f"images/{source}/{key}/{filename}"


def download_one(
    client: poc.HttpClient,
    url: str,
    dest_stem: Path,
    referer: str | None,
) -> dict[str, Any]:
    headers = {"Accept": IMAGE_ACCEPT}
    if referer:
        headers["Referer"] = referer
    try:
        response = client.get(encode_url(url), headers=headers, timeout=45)
    except (SSLError, Timeout, RequestException) as exc:
        return {"ok": False, "error": str(exc), "source_url": url}
    data = response.content or b""
    ctype = response.headers.get("Content-Type")
    fmt = sniff_format(data, ctype)
    dims = poc.image_dims(data) if data else None
    if response.status_code != 200 or not fmt or len(data) < MIN_BYTES:
        return {
            "ok": False,
            "error": f"status={response.status_code} bytes={len(data)} type={ctype}",
            "source_url": url,
        }
    dest = dest_stem.with_suffix(f".{fmt}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    row: dict[str, Any] = {
        "ok": True,
        "file": dest.name,
        "bytes": len(data),
        "content_type": ctype or f"image/{fmt}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_url": url,
    }
    if dims:
        row["width"], row["height"] = dims
    return row


def select_images(gallery: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    kept = [item for item in gallery.get("images") or [] if keep_image(item)]
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in kept:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)
    if limit is not None:
        unique = unique[:limit]
    return unique


def download_gallery(
    client: poc.HttpClient,
    gallery: dict[str, Any],
    out_dir: Path,
    *,
    resume: bool,
    limit_images: int | None,
    delay_min: float,
    delay_max: float,
) -> dict[str, Any]:
    source = gallery["source"]
    key = gallery_key(gallery)
    folder = out_dir / "images" / source / key
    meta_path = folder / "gallery.json"
    items = select_images(gallery, limit_images)
    if source == "ikco":
        page = gallery.get("source_url")
        if page:
            try:
                client.get(page, headers={"Referer": "https://www.ikco.ir/"})
            except (SSLError, Timeout, RequestException) as exc:
                LOGGER.warning("IKCO warmup failed %s: %s", page, exc)

    existing_by_url: dict[str, dict[str, Any]] = {}
    if resume and meta_path.exists():
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8"))
            for row in prev.get("images") or []:
                if row.get("source_url") and row.get("file"):
                    existing_by_url[row["source_url"]] = row
        except (OSError, json.JSONDecodeError):
            existing_by_url = {}

    downloaded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen_hash: set[str] = set()
    index = 1
    for item in items:
        url = item["url"]
        prev = existing_by_url.get(url)
        if resume and prev and prev.get("file"):
            dest = folder / prev["file"]
            if dest.exists() and dest.stat().st_size >= MIN_BYTES:
                row = dict(prev)
                row["kind"] = item.get("kind") or "gallery"
                row["sort"] = index
                if row.get("sha256"):
                    if row["sha256"] in seen_hash:
                        continue
                    seen_hash.add(row["sha256"])
                downloaded.append(row)
                index += 1
                continue
        stem = folder / f"{index:04d}"
        result = download_one(client, url, stem, gallery.get("source_url"))
        if delay_max > 0:
            time.sleep(random.uniform(min(delay_min, delay_max), max(delay_min, delay_max)))
        if not result.get("ok"):
            LOGGER.warning("fail %s %s %s", source, key, result.get("error"))
            failed.append({"source_url": url, "error": result.get("error")})
            continue
        digest = result.get("sha256")
        if digest and digest in seen_hash:
            (folder / result["file"]).unlink(missing_ok=True)
            continue
        if digest:
            seen_hash.add(digest)
        row = {
            "file": result["file"],
            "sort": index,
            "kind": item.get("kind") or "gallery",
            "bytes": result["bytes"],
            "content_type": result.get("content_type"),
            "sha256": digest,
            "source_url": url,
        }
        if result.get("width"):
            row["width"] = result["width"]
            row["height"] = result["height"]
        downloaded.append(row)
        index += 1
        LOGGER.info("saved %s/%s/%s (%s bytes)", source, key, row["file"], row["bytes"])

    meta = {
        "source": source,
        "source_name": gallery.get("source_name"),
        "name": gallery.get("name"),
        "slug": gallery.get("slug"),
        "gallery_key": key,
        "source_url": gallery.get("source_url"),
        "downloaded_at": now_iso(),
        "image_count": len(downloaded),
        "failed_count": len(failed),
        "images": downloaded,
        "failed": failed,
    }
    folder.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def pick_gallery_for_entity(
    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    def score(item: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> tuple[int, int]:
        match, gallery, meta = item
        n = len(meta.get("images") or [])
        trim = (match.get("trim") or "").lower()
        slug = (gallery.get("slug") or "").lower()
        name = (gallery.get("name") or "").lower()
        bonus = 0
        if "پلاس" in trim and ("plus" in slug or "پلاس" in name):
            bonus = 50
        if "پرو" in trim and "pro" in slug:
            bonus = 40
        return (n + bonus, n)

    return max(candidates, key=score)


def build_manifest(
    matches: dict[str, Any],
    galleries_by_url: dict[tuple[str, str], dict[str, Any]],
    gallery_meta: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    by_entity: dict[int, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for result in matches.get("results") or []:
        if result.get("status") != "matched":
            continue
        gallery = galleries_by_url.get((result.get("source"), result.get("source_url")))
        meta = gallery_meta.get((result.get("source"), result.get("source_url")))
        if not gallery or not meta:
            continue
        for match in result.get("matches") or []:
            entity_id = int(match["entity_id"])
            by_entity[entity_id].append((match, gallery, meta))

    entities: list[dict[str, Any]] = []
    for entity_id, candidates in sorted(by_entity.items()):
        match, gallery, meta = pick_gallery_for_entity(candidates)
        key = meta["gallery_key"]
        source = gallery["source"]
        images = []
        for row in meta.get("images") or []:
            images.append(
                {
                    "file": relative_file(source, key, row["file"]),
                    "sort": row.get("sort"),
                    "kind": row.get("kind") or "gallery",
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "bytes": row.get("bytes"),
                    "content_type": row.get("content_type"),
                    "source_url": row.get("source_url"),
                }
            )
        cover = images[0]["file"] if images else None
        entities.append(
            {
                "entity_id": entity_id,
                "brand": match.get("brand"),
                "model": match.get("model"),
                "trim": match.get("trim") or "",
                "slug": match.get("slug"),
                "source": source,
                "source_name": gallery.get("source_name"),
                "source_page": gallery.get("source_url"),
                "gallery_name": gallery.get("name"),
                "gallery_key": key,
                "cover": cover,
                "image_count": len(images),
                "images": images,
            }
        )

    with_images = [e for e in entities if e["image_count"] > 0]
    return {
        "generated_at": now_iso(),
        "version": 1,
        "image_root": "images",
        "usage": (
            "Copy the images/ folder next to this JSON. "
            "Each entities[].images[].file is relative to this directory. "
            "Use cover as the profile hero; images[] (sorted by sort) as the gallery."
        ),
        "stats": {
            "entity_count": len(entities),
            "entities_with_images": len(with_images),
            "entities_missing_images": len(entities) - len(with_images),
            "image_files": sum(e["image_count"] for e in with_images),
            "unique_files": len({img["file"] for e in with_images for img in e["images"]}),
        },
        "entities": entities,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=Path, default=DEFAULT_MATCHES)
    parser.add_argument("--galleries", type=Path, default=DEFAULT_GALLERIES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit-galleries", type=int, default=None)
    parser.add_argument("--limit-images", type=int, default=None)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--delay-min", type=float, default=0.15)
    parser.add_argument("--delay-max", type=float, default=0.4)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _configure_logging(args.verbose)
    matches = json.loads(args.matches.read_text(encoding="utf-8"))
    raw_galleries = load_jsonl(args.galleries)
    galleries_by_url = {(g["source"], g["source_url"]): g for g in raw_galleries}

    wanted: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result in matches.get("results") or []:
        if result.get("status") != "matched":
            continue
        if args.source and result.get("source") != args.source:
            continue
        key = (result.get("source"), result.get("source_url"))
        gallery = galleries_by_url.get(key)
        if not gallery or key in seen:
            continue
        seen.add(key)
        wanted.append(gallery)
    if args.limit_galleries is not None:
        wanted = wanted[: args.limit_galleries]

    LOGGER.info("galleries to download: %s", len(wanted))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = poc.HttpClient()
    gallery_meta: dict[tuple[str, str], dict[str, Any]] = {}
    ok = fail = 0
    for gallery in wanted:
        meta = download_gallery(
            client,
            gallery,
            args.out_dir,
            resume=args.resume,
            limit_images=args.limit_images,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
        )
        gallery_meta[(gallery["source"], gallery["source_url"])] = meta
        n = meta.get("image_count") or 0
        if n:
            ok += 1
        else:
            fail += 1
        LOGGER.info(
            "gallery %s/%s images=%s failed=%s",
            gallery["source"],
            meta.get("gallery_key"),
            n,
            meta.get("failed_count") or 0,
        )

    # Include already-downloaded galleries when rebuilding after a partial run.
    if args.resume:
        for gallery in galleries_by_url.values():
            key = (gallery["source"], gallery["source_url"])
            if key in gallery_meta:
                continue
            meta_path = args.out_dir / "images" / gallery["source"] / gallery_key(gallery) / "gallery.json"
            if meta_path.exists():
                gallery_meta[key] = json.loads(meta_path.read_text(encoding="utf-8"))

    manifest = build_manifest(matches, galleries_by_url, gallery_meta)
    manifest_path = args.out_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    stats = manifest["stats"]
    LOGGER.info(
        "Wrote %s entities=%s with_images=%s unique_files=%s galleries_ok=%s empty=%s",
        manifest_path,
        stats["entity_count"],
        stats["entities_with_images"],
        stats["unique_files"],
        ok,
        fail,
    )
    return 0 if stats["entities_with_images"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
