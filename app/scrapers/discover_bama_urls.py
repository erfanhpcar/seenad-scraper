#!/usr/bin/env python3
"""
Discover Bama.ir car-reviews technical specification page URLs.

Primary source (default):
  GET https://bama.ir/sitemap/research
  Official sitemap lists every trim/spec page (including sibling trims that
  brand index pages omit). Brand SSR pages only link the default trim per model;
  extra trims appear as tabs on the spec page itself.

Optional HTML fallback (``--html-fallback``):
  1. Fetch the car-reviews hub (``/car-reviews``) — SSR lists every brand tile/link.
  2. Collect brand index paths matching ``/car-reviews/{brand}``.
  3. Fetch each brand page — lists default trim/spec pages for that marque.
  4. Filter links matching ``/car-reviews/{brand}/{slug}-specs-{id}…``.

Output: unique absolute URLs written to ``bama_specs_urls.txt`` (one per line).

Usage::

    python app/scrapers/discover_bama_urls.py
    python app/scrapers/discover_bama_urls.py --html-fallback --max-brands 10
    docker exec -i seenad_backend python3 app/scrapers/discover_bama_urls.py
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

BAMA_ORIGIN = "https://bama.ir"
DEFAULT_HUB_URL = f"{BAMA_ORIGIN}/car-reviews"
DEFAULT_SITEMAP_URL = f"{BAMA_ORIGIN}/sitemap/research"
DEFAULT_OUTPUT = Path("bama_specs_urls.txt")
REQUEST_TIMEOUT_S = 30.0
DEFAULT_DELAY_S = 0.15

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": f"{BAMA_ORIGIN}/",
}

BRAND_PATH_RE = re.compile(r"^/car-reviews/[a-z0-9-]+$", re.IGNORECASE)
SPEC_PATH_RE = re.compile(
    r"^/car-reviews/[a-z0-9-]+/[a-z0-9.-]+-specs-[a-z0-9.-]+$",
    re.IGNORECASE,
)


def _normalize_href(href: str) -> str | None:
    """Return a canonical path like ``/car-reviews/…`` or ``None`` if not applicable."""
    raw = href.strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:")):
        return None

    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        if parsed.netloc and "bama.ir" not in parsed.netloc.lower():
            return None
        path = parsed.path or ""
    else:
        path = raw.split("?")[0].split("#")[0]

    if not path.startswith("/"):
        path = f"/{path}"

    return path.rstrip("/") or path


def _absolute_spec_url(path: str) -> str:
    return urljoin(BAMA_ORIGIN, path)


def _extract_paths(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    paths: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        normalized = _normalize_href(anchor["href"])
        if normalized:
            paths.add(normalized)
    return paths


def _brand_paths(paths: set[str]) -> set[str]:
    return {p for p in paths if BRAND_PATH_RE.fullmatch(p)}


def _spec_paths(paths: set[str]) -> set[str]:
    return {p for p in paths if SPEC_PATH_RE.fullmatch(p)}


def _fetch_html(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    if response.status_code != 200:
        raise httpx.HTTPStatusError(
            f"GET {url} failed with status {response.status_code}",
            request=response.request,
            response=response,
        )
    return response.text


def _extract_sitemap_spec_paths(xml: str) -> set[str]:
    """Parse ``<loc>`` entries from the research sitemap into spec paths."""
    paths: set[str] = set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        normalized = _normalize_href(loc)
        if normalized and SPEC_PATH_RE.fullmatch(normalized):
            paths.add(normalized)
    return paths


def _discover_from_sitemap(client: httpx.Client, sitemap_url: str) -> set[str]:
    LOGGER.info("Fetching sitemap: %s", sitemap_url)
    xml = _fetch_html(client, sitemap_url)
    paths = _extract_sitemap_spec_paths(xml)
    LOGGER.info("Sitemap parsed: %d spec URLs", len(paths))
    if not paths:
        LOGGER.warning("Sitemap returned zero spec URLs — XML may have changed")
    return paths


def _discover_from_html(
    client: httpx.Client,
    hub_url: str,
    *,
    max_brands: int | None,
    delay_s: float,
) -> set[str]:
    spec_paths: set[str] = set()
    LOGGER.info("Fetching hub: %s", hub_url)
    hub_html = _fetch_html(client, hub_url)
    hub_paths = _extract_paths(hub_html)
    spec_paths |= _spec_paths(hub_paths)
    brand_paths = _brand_paths(hub_paths)
    LOGGER.info(
        "Hub parsed: %d brand links, %d spec links (SSR)",
        len(brand_paths),
        len(spec_paths),
    )

    brands_to_visit = sorted(brand_paths)
    if max_brands is not None:
        brands_to_visit = brands_to_visit[:max_brands]
        LOGGER.info("Limiting crawl to %d brand pages (--max-brands)", len(brands_to_visit))

    for index, brand_path in enumerate(brands_to_visit, start=1):
        brand_url = _absolute_spec_url(brand_path)
        LOGGER.info("Brand %d/%d: %s", index, len(brands_to_visit), brand_path)
        try:
            brand_html = _fetch_html(client, brand_url)
        except httpx.HTTPError as exc:
            LOGGER.warning("Skipping %s — fetch failed: %s", brand_path, exc)
            continue

        page_paths = _extract_paths(brand_html)
        new_specs = _spec_paths(page_paths)
        added = len(new_specs - spec_paths)
        spec_paths |= new_specs
        LOGGER.debug(
            "  %d spec links on page (%d new), running total %d",
            len(new_specs),
            added,
            len(spec_paths),
        )

        if delay_s > 0 and index < len(brands_to_visit):
            time.sleep(delay_s)

    return spec_paths


def discover_spec_urls(
    hub_url: str = DEFAULT_HUB_URL,
    *,
    sitemap_url: str | None = DEFAULT_SITEMAP_URL,
    html_fallback: bool = False,
    max_brands: int | None = None,
    delay_s: float = DEFAULT_DELAY_S,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> list[str]:
    """
    Discover spec page URLs via the research sitemap (default) and optional HTML crawl.

    Returns sorted unique absolute URLs.
    """
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = hub_url

    spec_paths: set[str] = set()

    with httpx.Client(
        headers=headers,
        timeout=timeout_s,
        follow_redirects=True,
    ) as client:
        if sitemap_url:
            spec_paths |= _discover_from_sitemap(client, sitemap_url)
        if html_fallback or not sitemap_url:
            spec_paths |= _discover_from_html(
                client,
                hub_url,
                max_brands=max_brands,
                delay_s=delay_s,
            )

    urls = sorted({_absolute_spec_url(p) for p in spec_paths})
    LOGGER.info("Discovery complete: %d unique spec URLs", len(urls))
    return urls


def save_urls(urls: list[str], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    LOGGER.info("Saved %d URLs to %s", len(urls), output_path.resolve())
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover Bama.ir car-reviews technical specification page URLs.",
    )
    parser.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Car-reviews hub URL (default: {DEFAULT_HUB_URL})",
    )
    parser.add_argument(
        "--sitemap-url",
        default=DEFAULT_SITEMAP_URL,
        help=f"Research sitemap URL (default: {DEFAULT_SITEMAP_URL})",
    )
    parser.add_argument(
        "--no-sitemap",
        action="store_true",
        help="Skip the sitemap and discover only via hub → brand HTML.",
    )
    parser.add_argument(
        "--html-fallback",
        action="store_true",
        help="Also crawl hub → brand pages (brand pages omit sibling trims).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output text file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--max-brands",
        type=int,
        default=None,
        metavar="N",
        help="Only crawl the first N brand pages (for quick tests)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_S,
        help=f"Seconds between brand page requests (default: {DEFAULT_DELAY_S})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_S,
        help=f"HTTP timeout in seconds (default: {REQUEST_TIMEOUT_S})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s | %(message)s",
    )

    try:
        urls = discover_spec_urls(
            args.hub_url,
            sitemap_url=None if args.no_sitemap else args.sitemap_url,
            html_fallback=args.html_fallback,
            max_brands=args.max_brands,
            delay_s=args.delay,
            timeout_s=args.timeout,
        )
    except httpx.HTTPError as exc:
        LOGGER.error("Discovery failed: %s", exc)
        return 1

    if not urls:
        LOGGER.error("Empty result set — not writing %s", args.output)
        return 1

    save_urls(urls, args.output)
    print(f"Discovered {len(urls)} spec URLs → {args.output.resolve()}")
    print("Sample:")
    for sample in urls[:5]:
        print(f"  {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
