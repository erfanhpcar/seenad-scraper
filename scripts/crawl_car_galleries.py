#!/usr/bin/env python3
"""
Crawler: official OEM car-gallery image URLs for SeenAd entities.

Uses extractors validated by scripts/probe_car_gallery_poc.py.
Does not bulk-download binaries — records source page + image URLs.

Sources: kermanmotor, mvm, fownix, bahman, lamari, mammut, saipa, ikco

Usage:
  python scripts/crawl_car_galleries.py --limit-per-source 2 -v \\
      --output data/raw_car_galleries_smoke.jsonl
  python scripts/crawl_car_galleries.py --source saipa --limit 5 -v
  python scripts/crawl_car_galleries.py --resume
  docker exec -i seenad_backend python3 scripts/crawl_car_galleries.py \\
      --limit-per-source 2 -v --output data/raw_car_galleries_smoke.jsonl

Full run:
  nohup docker exec -i seenad_backend python3 scripts/crawl_car_galleries.py --resume -v \\
    > data/car_galleries_crawl.log 2>&1 &

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import urllib3

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import probe_car_gallery_poc as poc  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGGER = logging.getLogger("car-galleries-crawler")
DEFAULT_OUTPUT = Path("data/raw_car_galleries.jsonl")
SOURCE_NAME = "oem-car-galleries"
WP_TINY_RE = re.compile(r"-(\d+)x(\d+)\.(?:jpe?g|png|webp|avif)$", re.I)
SLUG_TOKEN_RE = re.compile(r"[^a-z0-9]+", re.I)

KERMAN_JAC_SLUGS = {"s3", "s5", "j3", "j4", "j5"}
SAIPA_BRAND_MAP = {
    "saipa": None,  # resolved from slug
    "parskhodro": None,
    "zamyad": "زامیاد",
    "saipadiesel": "فوتون",
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sleep_polite(delay_min: float, delay_max: float) -> None:
    if delay_max <= 0:
        return
    time.sleep(random.uniform(min(delay_min, delay_max), max(delay_min, delay_max)))


def path_slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def slug_token_in_url(slug: str, url: str) -> bool:
    token = SLUG_TOKEN_RE.sub("", (slug or "").lower())
    if len(token) < 2:
        return False
    parts = [p for p in re.split(r"[^a-z0-9]+", urlparse(url).path.lower()) if p]
    return token in parts


def filter_images(source_id: str, images: list[dict[str, Any]], *, slug: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in images:
        url = item.get("url") or ""
        if not url or url in seen or poc.looks_skipped(url):
            continue
        tiny = WP_TINY_RE.search(url.split("?")[0])
        if tiny and int(tiny.group(1)) < 700:
            continue
        kind = item.get("kind") or "gallery"
        if source_id in {"mvm", "fownix"} and kind == "hero" and slug:
            if not slug_token_in_url(slug, url):
                continue
        if source_id == "lamari" and re.search(r"rectangle|rebage|vector-smart|specs-echo-copy", url, re.I):
            continue
        seen.add(url)
        row = {"url": url, "kind": kind}
        if item.get("folder"):
            row["folder"] = item["folder"]
        out.append(row)
    return out


def guess_brand_model(source_id: str, item: dict[str, Any], title: str | None) -> tuple[str | None, str | None]:
    name = poc.clean_text(item.get("name") or title or "") or ""
    slug = (item.get("slug") or path_slug(item.get("url") or "")).strip()
    slug_l = slug.lower()

    if source_id == "kermanmotor":
        brand = "جک" if slug_l in KERMAN_JAC_SLUGS else "کی ام سی"
        model = slug.upper().replace("EAGLE", "ایگل")
        if slug_l == "eagle":
            model = "ایگل"
        elif slug_l == "ej7plus":
            model = "eJ7"
        elif slug_l == "ej7":
            model = "eJ7"
        return brand, model

    if source_id == "mvm":
        model = slug.replace("Pro", " PRO").replace("Cross", " کراس")
        return "ام وی ام", model

    if source_id == "fownix":
        return "فونیکس", name or slug

    if source_id == "lamari":
        return "لاماری", name.replace("لاماری ", "").strip() or slug

    if source_id == "mammut":
        pretty = {
            "id-4": "ID.4 Crozz",
            "tayron-l": "تایرون L",
            "tiguan-l-pro": "تیگوان L",
            "tiguan-l-outstanding": "تیگوان L",
            "t-roc": "تی راک",
            "golf-gti-2": "گلف",
            "new-vs7": "VS7",
        }
        return "فولکس", pretty.get(slug_l, name or slug)

    if source_id == "saipa":
        folder = (item.get("brand") or "").lower()
        mapping = [
            ("saina", "ساینا"),
            ("shahin", "شاهین"),
            ("quik", "کوییک"),
            ("quick", "کوییک"),
            ("atlas", "اطلس"),
            ("sahand", "سهند"),
            ("arya", "آریا"),
            ("tiba", "تیبا"),
            ("pars-noa", "پارس نوآ"),
            ("saipa151", "زامیاد"),
            ("151", "زامیاد"),
            ("padra", "زامیاد"),
            ("karoon", "زامیاد"),
            ("zamyad", "زامیاد"),
            ("c3-xr", "سیتروئن"),
        ]
        brand = SAIPA_BRAND_MAP.get(folder)
        model = slug
        for key, br in mapping:
            if key in slug_l:
                brand = brand or br
                break
        if "saina-s" in slug_l or slug_l.endswith("-s") and "saina" in slug_l:
            model = "S"
        elif "saina" in slug_l:
            model = slug
        return brand, model

    if source_id == "bahman":
        pairs = [
            ("فیدلیتی", "فیدلیتی"),
            ("دیگنیتی", "دیگنیتی"),
            ("ریسپکت", "ریسپکت"),
            ("شوال", "شوال"),
            ("اینوی", "اینوی"),
            ("اینرودز", "اینرودز"),
            ("کاپرا", "کاپرا"),
            ("مزدا", "مزدا"),
            ("هاوال", "هاوال"),
        ]
        for token, brand in pairs:
            if token in name:
                rest = name.replace(token, "", 1).strip()
                return brand, rest or name
        return None, name

    if source_id == "ikco":
        cleaned = re.sub(r"گالری تصاویر|در یک نگاه|-+\s*", " ", name)
        cleaned = poc.clean_text(cleaned) or name
        brands = [
            ("ری را", "ری را"),
            ("هایما", "هایما"),
            ("دنا", "دنا"),
            ("رانا", "رانا"),
            ("سمند", "سمند"),
            ("سورن", "سمند"),
            ("آریسان", "آریسان"),
            ("پژو", "پژو"),
            ("پارس", "پژو"),
            ("لونا", "لونا"),
            ("بستیون", "بستیون"),
            ("تارا", "تارا"),
        ]
        for token, brand in brands:
            if re.search(rf"(^|[\s]){re.escape(token)}([\s]|$)", cleaned):
                rest = cleaned.replace(token, "", 1).strip()
                return brand, rest or token
        return None, cleaned

    return None, name or slug


def unique_listing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = (item.get("url") or "").split("#")[0]
        pid = str(item.get("product_id") or "")
        key = pid or url
        if not key or key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item["url"] = url
        out.append(item)
    return out


class GalleryCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        resume: bool,
        limit: int | None,
        limit_per_source: int | None,
        source_ids: list[str] | None,
        delay_min: float,
        delay_max: float,
    ) -> None:
        self.output_path = output_path
        self.resume = resume
        self.limit = limit
        self.limit_per_source = limit_per_source
        self.source_ids = set(source_ids or [])
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.client = poc.HttpClient()
        self._done_keys: set[str] = set()

    def _resume_key(self, source_id: str, url: str) -> str:
        return f"{source_id}|{url.split('#')[0]}"

    def _load_resume(self) -> None:
        if not self.resume or not self.output_path.exists():
            return
        with self.output_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Skip bad JSONL line %s", line_no)
                    continue
                key = self._resume_key(str(row.get("source") or ""), str(row.get("source_url") or ""))
                if key != "|":
                    self._done_keys.add(key)
        LOGGER.info("Resume: loaded %s existing record(s)", len(self._done_keys))

    def _append(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def run(self) -> int:
        self._load_resume()
        if not self.resume and self.output_path.exists():
            LOGGER.info("Removing existing output (no --resume): %s", self.output_path)
            self.output_path.unlink()

        specs = [
            spec
            for spec in poc.SOURCE_SPECS
            if not self.source_ids or spec["id"] in self.source_ids
        ]
        if not specs:
            LOGGER.error("No sources selected")
            return 1

        success = 0
        skipped = 0
        failed = 0
        empty = 0

        for spec in specs:
            source_id = spec["id"]
            LOGGER.info("=== %s (%s) listing %s ===", spec["name"], source_id, spec["listing_url"])
            listing = self.client.fetch_html(spec["listing_url"])
            if listing.get("soup") is None or listing.get("status") != 200 or listing.get("blocked"):
                LOGGER.error(
                    "Listing failed source=%s status=%s blocked=%s err=%s",
                    source_id,
                    listing.get("status"),
                    listing.get("blocked"),
                    listing.get("error"),
                )
                failed += 1
                continue
            items = unique_listing(spec["listing"](self.client, listing["soup"], listing["url"]))
            if self.limit_per_source is not None:
                items = items[: max(0, self.limit_per_source)]
            LOGGER.info("  models=%s", len(items))
            if not items:
                LOGGER.warning("  empty listing for %s", source_id)
                empty += 1
                continue

            per_source_ok = 0
            for item in items:
                if self.limit is not None and success >= self.limit:
                    LOGGER.info("Reached --limit=%s", self.limit)
                    LOGGER.info(
                        "Done early. success=%s skipped=%s failed=%s empty=%s output=%s",
                        success,
                        skipped,
                        failed,
                        empty,
                        self.output_path,
                    )
                    return 0 if success else 1

                detail_url = (
                    spec["rewrite_detail"](item["url"]) if spec.get("rewrite_detail") else item["url"]
                )
                key = self._resume_key(source_id, detail_url)
                if key in self._done_keys:
                    skipped += 1
                    continue

                sleep_polite(self.delay_min, self.delay_max)
                page = self.client.fetch_html(detail_url, referer=spec["listing_url"])
                if page.get("soup") is None or page.get("status") != 200 or page.get("blocked"):
                    LOGGER.warning(
                        "  detail fail %s status=%s blocked=%s err=%s",
                        detail_url,
                        page.get("status"),
                        page.get("blocked"),
                        page.get("error"),
                    )
                    failed += 1
                    continue

                slug = item.get("slug") or path_slug(item["url"])
                images = spec["gallery"](page["soup"], page["url"], page["html"])
                images = filter_images(source_id, images, slug=slug)
                title = page.get("title")
                brand, model = guess_brand_model(source_id, item, title)
                name = poc.clean_text(item.get("name") or title) or slug

                record = {
                    "source": source_id,
                    "source_name": spec["name"],
                    "name": name,
                    "brand": brand,
                    "model": model,
                    "slug": slug,
                    "source_url": page.get("url") or detail_url,
                    "listing_url": spec["listing_url"],
                    "image_count": len(images),
                    "images": images,
                    "crawled_at": _now_iso(),
                    "raw_payload": {
                        "listing_name": item.get("name"),
                        "product_id": item.get("product_id"),
                        "page_title": title,
                        "html_bytes": page.get("bytes"),
                    },
                }
                self._append(record)
                self._done_keys.add(key)
                success += 1
                per_source_ok += 1
                if not images:
                    empty += 1
                    LOGGER.warning("  %s: 0 images", name)
                else:
                    LOGGER.info("  %s: %s images brand=%s model=%s", name, len(images), brand, model)

            if per_source_ok == 0 and items:
                LOGGER.warning("No successful records for %s", source_id)

        LOGGER.info(
            "Done. success=%s skipped=%s failed=%s empty_gallery_or_listing=%s output=%s",
            success,
            skipped,
            failed,
            empty,
            self.output_path,
        )
        if success == 0:
            LOGGER.error("Empty result set")
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl official OEM car galleries")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="max records total")
    parser.add_argument("--limit-per-source", type=int, default=None)
    parser.add_argument("--source", action="append", help="source id (repeatable)")
    parser.add_argument("--delay-min", type=float, default=0.7)
    parser.add_argument("--delay-max", type=float, default=1.5)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _configure_logging(args.verbose)
    crawler = GalleryCrawler(
        output_path=Path(args.output),
        resume=args.resume,
        limit=args.limit,
        limit_per_source=args.limit_per_source,
        source_ids=args.source,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )
    return crawler.run()


if __name__ == "__main__":
    sys.exit(main())
