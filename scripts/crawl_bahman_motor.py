#!/usr/bin/env python3
"""
Crawler: Bahman Motor (بهمن موتور) authorized dealership network.

The public page https://bahman.ir/branch is an empty UI shell. Branch, province,
and city data are served from panel.bahman.ir GraphQL (same API the site JS uses).

Usage:
  python scripts/crawl_bahman_motor.py
  python scripts/crawl_bahman_motor.py --output data/raw_bahman_motor_dealers.json -v
  python scripts/crawl_bahman_motor.py --format table
  python scripts/crawl_bahman_motor.py --token "5|..."

Dependencies (project requirements.txt):
  requests
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3
from requests import Session
from requests.exceptions import RequestException

LOGGER = logging.getLogger("bahman-motor-crawler")

GRAPHQL_ENDPOINT = "https://panel.bahman.ir/graphql"
BUNDLE_JS_URL = "https://panel.bahman.ir/template/bahman/bundle-v1.js"
BRANCH_PAGE_URL = "https://bahman.ir/branch"
DEFAULT_OUTPUT = Path("data/raw_bahman_motor_dealers.json")

DEFAULT_BEARER_TOKEN = "5|SGuVpWYHrITQt8auyQY9mgpWH1j4ynUFrHbNCMJc"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BRANCH_LIST_FIRST = 1000
DEFAULT_TIMEOUT_S = 120.0

BEARER_TOKEN_RE = re.compile(r'authorization="Bearer\s+([^"]+)"', re.IGNORECASE)
BRANCH_CODE_RE = re.compile(r"^[\d۰-۹٠-٩]+")
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")

# One round-trip: nested province/city avoid separate ~60s lookup calls on this host.
BRANCH_LIST_QUERY = """
query BranchList {
  branchList(
    first: %(first)s
    where: { column: STATUS, operator: IN, value: ["0", "1"] }
  ) {
    paginatorInfo { total count }
    data {
      id
      title
      address
      status
      province_id
      city_id
      google_map
      province { id name }
      city { id name }
    }
  }
}
""".strip() % {"first": BRANCH_LIST_FIRST}


@dataclass
class DealershipRecord:
    name: str | None
    code: str | None
    city: str | None
    province: str | None
    address: str | None
    phone_numbers: list[str] = field(default_factory=list)
    google_map: str | None = None
    status: int | None = None
    province_id: str | None = None
    city_id: str | None = None
    source: str = "bahman.ir"
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def clean_farsi_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def normalize_embed_field(value: str | None) -> str | None:
    """Preserve map embed HTML/URLs without Persian normalization."""
    if value is None:
        return None
    text = html_lib.unescape(value).strip()
    return text or None


def extract_branch_code(title: str | None) -> str | None:
    """Parse leading dealer code from titles like '1011- احمد رحیمیان'."""
    if not title:
        return None
    normalized = clean_farsi_text(title)
    if not normalized:
        return None
    match = BRANCH_CODE_RE.match(normalized)
    if not match:
        return None
    code = match.group(0).strip("-").strip()
    return code or None


def resolve_bearer_token(session: Session, timeout: float, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()

    LOGGER.debug("Fetching bearer token from %s", BUNDLE_JS_URL)
    response = session.get(BUNDLE_JS_URL, timeout=timeout, verify=False)
    response.raise_for_status()
    match = BEARER_TOKEN_RE.search(response.text)
    if match:
        token = match.group(1).strip()
        LOGGER.info("Resolved bearer token from bundle-v1.js")
        return token

    LOGGER.warning(
        "Could not extract token from bundle; using embedded default (may be stale)"
    )
    return DEFAULT_BEARER_TOKEN


class BahmanGraphQLClient:
    def __init__(self, session: Session, bearer_token: str, timeout: float) -> None:
        self._session = session
        self._timeout = timeout
        self._headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": "https://bahman.ir",
            "Referer": BRANCH_PAGE_URL,
            "Authorization": f"Bearer {bearer_token}",
        }

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        response = self._session.post(
            GRAPHQL_ENDPOINT,
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
            verify=False,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        errors = body.get("errors")
        if errors:
            messages = "; ".join(
                str(item.get("message", item)) for item in errors if isinstance(item, dict)
            )
            raise RuntimeError(f"GraphQL errors: {messages or errors}")
        data = body.get("data")
        if data is None:
            raise RuntimeError(f"GraphQL response missing data: {body!r}")
        return data


class BahmanMotorCrawler:
    def __init__(self, client: BahmanGraphQLClient) -> None:
        self._client = client

    @staticmethod
    def _nested_name(node: dict[str, Any] | None) -> str | None:
        if not node:
            return None
        return clean_farsi_text(node.get("name"))

    def crawl(self) -> list[DealershipRecord]:
        LOGGER.info("Fetching active branches (status IN 0, 1)")
        data = self._client.execute(BRANCH_LIST_QUERY)
        block = data.get("branchList") or {}
        rows = block.get("data") or []
        paginator = block.get("paginatorInfo") or {}
        api_total = paginator.get("total")
        LOGGER.info(
            "Branches: retrieved %s rows (API total: %s, count: %s)",
            len(rows),
            api_total,
            paginator.get("count"),
        )
        if api_total is not None and len(rows) < int(api_total):
            raise RuntimeError(
                f"branchList returned {len(rows)} rows but API total is {api_total}; "
                f"increase BRANCH_LIST_FIRST ({BRANCH_LIST_FIRST})"
            )

        records: list[DealershipRecord] = []
        for row in rows:
            province_id = row.get("province_id")
            city_id = row.get("city_id")
            province_key = (
                str(province_id).strip() if province_id is not None else None
            )
            city_key = str(city_id).strip() if city_id is not None else None

            title = clean_farsi_text(row.get("title"))
            records.append(
                DealershipRecord(
                    name=title,
                    code=extract_branch_code(title),
                    city=self._nested_name(row.get("city")),
                    province=self._nested_name(row.get("province")),
                    address=clean_farsi_text(row.get("address")),
                    google_map=normalize_embed_field(row.get("google_map")),
                    status=row.get("status"),
                    province_id=province_key,
                    city_id=city_key,
                )
            )

        records.sort(
            key=lambda row: (
                row.province or "",
                row.city or "",
                row.code or "",
                row.name or "",
            )
        )
        return records


def print_table(records: list[DealershipRecord]) -> None:
    if not records:
        print("No dealerships extracted.")
        return

    headers = ("code", "name", "province", "city", "status")
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.code or "-",
                (record.name or "-")[:40],
                (record.province or "-")[:14],
                (record.city or "-")[:16],
                str(record.status) if record.status is not None else "-",
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(list(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt_row(row))
    print()
    print(f"Total: {len(records)} dealerships")


def write_json(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Bahman Motor dealerships via panel.bahman.ir GraphQL."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Write JSON array to this path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "both"),
        default="both",
        help="Console output format (default: both)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GraphQL bearer token (default: extract from bundle-v1.js)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _configure_logging(args.verbose)

    session = requests.Session()
    try:
        bearer = resolve_bearer_token(session, args.timeout, args.token)
        client = BahmanGraphQLClient(session, bearer, args.timeout)
        records = BahmanMotorCrawler(client).crawl()
    except (RequestException, RuntimeError, json.JSONDecodeError) as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    LOGGER.info("Wrote %d records to %s", len(records), args.output.resolve())

    if args.format in ("table", "both"):
        print_table(records)

    if args.format in ("json", "both"):
        print(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
