#!/usr/bin/env python3
"""
Crawl Bama calculator taxonomy via official CAD APIs (same host axios uses: https://bama.ir)
and write results to a JSON file (no database writes).

Endpoints (discovered from Nuxt bundles + Network parity checks):
  GET /cad/api/BasicInfo/brands
  GET /cad/api/BasicInfo/models?brandId=<int>
  GET /cad/api/BasicInfo/years?BrandId=<int>&ModelId=<int>
  GET /cad/api/BasicInfo/trims?BrandId=<int>&ModelId=<int>&ModelYear=<int Jalali>

Persian calendar years from ``years`` / ``ModelYear`` are converted to Gregorian calendar years
for ``production_year_start`` / ``production_year_end`` (calendar year of Farvardin 1 … last day of Esfand).

Rows with empty ``trim`` are kept when Bama only has brand+model (no trim variants).

Supports retry/backoff on transient HTTP errors and checkpoint resume so a long crawl
can recover after timeouts (e.g. 504) without restarting from scratch.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, Timeout

LOGGER = logging.getLogger("crawl-bama-reference")

BAMA_ORIGIN = "https://bama.ir"
BRANDS_PATH = "/cad/api/BasicInfo/brands"
MODELS_PATH = "/cad/api/BasicInfo/models"
YEARS_PATH = "/cad/api/BasicInfo/years"
TRIMS_PATH = "/cad/api/BasicInfo/trims"

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "bama_calculator_reference.json"
DEFAULT_CHECKPOINT = Path(__file__).resolve().parents[1] / "bama_calculator_checkpoint.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://bama.ir/calculator",
    "Origin": "https://bama.ir",
}

# Transient statuses worth retrying (Bama CDN / gateway flaps).
RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def _normalize_label(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _jalali_bounds_to_gregorian_year_span(j_min: int, j_max: int) -> tuple[int, int]:
    """Map inclusive Jalali production-year span to Gregorian calendar-year endpoints.

    Farvardin 1 of Jalali year Y falls in Gregorian year Y+621;
    Esfand 29/30 of Jalali year Y falls in Gregorian year Y+622.
    """
    return j_min + 621, j_max + 622


def _key_to_str(key: tuple[str, str, str]) -> str:
    return json.dumps(list(key), ensure_ascii=False)


def _str_to_key(raw: str) -> tuple[str, str, str]:
    parts = json.loads(raw)
    return str(parts[0]), str(parts[1]), str(parts[2])


class BamaCadClient:
    def __init__(
        self,
        session: requests.Session,
        delay_sec: float,
        *,
        max_retries: int = 8,
        backoff_base: float = 1.5,
        backoff_cap: float = 60.0,
        request_timeout: float = 90.0,
    ) -> None:
        self._session = session
        self._delay_sec = delay_sec
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._request_timeout = request_timeout

    def _sleep(self) -> None:
        if self._delay_sec > 0:
            time.sleep(self._delay_sec)

    def _backoff_seconds(self, attempt: int) -> float:
        # attempt is 1-based after a failure
        return min(self._backoff_cap, self._backoff_base * (2 ** (attempt - 1)))

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        empty_on_http: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        url = f"{BAMA_ORIGIN}{path}"
        params = params or {}
        last_exc: BaseException | None = None

        for attempt in range(1, self._max_retries + 1):
            self._sleep()
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=HEADERS,
                    timeout=self._request_timeout,
                )
            except (Timeout, RequestsConnectionError) as exc:
                last_exc = exc
                wait = self._backoff_seconds(attempt)
                LOGGER.warning(
                    "Network error on %s params=%s attempt=%s/%s: %s — sleep %.1fs",
                    path,
                    params,
                    attempt,
                    self._max_retries,
                    exc,
                    wait,
                )
                if attempt >= self._max_retries:
                    break
                time.sleep(wait)
                continue

            if response.status_code in empty_on_http:
                LOGGER.warning(
                    "HTTP %s for %s params=%s — treating as empty data",
                    response.status_code,
                    path,
                    params,
                )
                return {"status": True, "data": []}

            if response.status_code in RETRYABLE_HTTP:
                wait = self._backoff_seconds(attempt)
                # Honor Retry-After when present (seconds).
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                LOGGER.warning(
                    "HTTP %s for %s params=%s attempt=%s/%s — sleep %.1fs",
                    response.status_code,
                    path,
                    params,
                    attempt,
                    self._max_retries,
                    wait,
                )
                last_exc = HTTPError(
                    f"{response.status_code} for url: {response.url}",
                    response=response,
                )
                if attempt >= self._max_retries:
                    break
                time.sleep(wait)
                continue

            try:
                response.raise_for_status()
            except HTTPError as exc:
                # Non-retryable HTTP (e.g. 400/401/403) — fail fast.
                raise

            payload = response.json()
            if not payload.get("status"):
                errs = payload.get("errors") or []
                raise RuntimeError(f"Bama API error on {path}: {errs}")
            return payload

        assert last_exc is not None
        raise last_exc

    def brands(self) -> list[dict[str, Any]]:
        data = self._get(BRANDS_PATH)["data"]
        return data if isinstance(data, list) else []

    def models(self, brand_id: int) -> list[dict[str, Any]]:
        data = self._get(MODELS_PATH, {"brandId": brand_id})["data"]
        return data if isinstance(data, list) else []

    def years(self, brand_id: int, model_id: int) -> list[int]:
        data = self._get(YEARS_PATH, {"BrandId": brand_id, "ModelId": model_id}, empty_on_http=(404,))[
            "data"
        ]
        if not isinstance(data, list):
            return []
        out: list[int] = []
        for row in data:
            if isinstance(row, dict) and row.get("value") is not None:
                try:
                    out.append(int(row["value"]))
                except (TypeError, ValueError):
                    continue
        return out

    def trims(self, brand_id: int, model_id: int, model_year_jalali: int) -> list[dict[str, Any]]:
        data = self._get(
            TRIMS_PATH,
            {"BrandId": brand_id, "ModelId": model_id, "ModelYear": model_year_jalali},
            empty_on_http=(404,),
        )["data"]
        return data if isinstance(data, list) else []


def _load_checkpoint(path: Path) -> tuple[set[int], defaultdict[tuple[str, str, str], set[int]]]:
    done_brand_ids: set[int] = set()
    trim_jalali_years: defaultdict[tuple[str, str, str], set[int]] = defaultdict(set)
    if not path.exists():
        return done_brand_ids, trim_jalali_years

    raw = json.loads(path.read_text(encoding="utf-8"))
    for bid in raw.get("done_brand_ids") or []:
        try:
            done_brand_ids.add(int(bid))
        except (TypeError, ValueError):
            continue
    for key_s, years in (raw.get("trim_jalali_years") or {}).items():
        try:
            key = _str_to_key(key_s)
        except (TypeError, ValueError, json.JSONDecodeError, IndexError):
            continue
        for y in years:
            try:
                trim_jalali_years[key].add(int(y))
            except (TypeError, ValueError):
                continue
    LOGGER.info(
        "Resumed checkpoint %s — done brands=%s trim-keys=%s",
        path,
        len(done_brand_ids),
        len(trim_jalali_years),
    )
    return done_brand_ids, trim_jalali_years


def _save_checkpoint(
    path: Path,
    done_brand_ids: set[int],
    trim_jalali_years: dict[tuple[str, str, str], set[int]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "done_brand_ids": sorted(done_brand_ids),
        "trim_jalali_years": {
            _key_to_str(k): sorted(v) for k, v in sorted(trim_jalali_years.items())
        },
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def crawl_tree(
    client: BamaCadClient,
    *,
    checkpoint_path: Path | None = None,
    resume: bool = True,
) -> dict[tuple[str, str, str], set[int]]:
    """
    Returns map (brand_label, model_label, trim_label) -> set of Jalali model years.

    ``trim_label`` may be ``""`` when Bama has only brand+model (no trim variants)
    for that year, or when the API returns a trim row with an empty label.
    """
    if checkpoint_path and resume:
        done_brand_ids, trim_jalali_years = _load_checkpoint(checkpoint_path)
    else:
        done_brand_ids = set()
        trim_jalali_years = defaultdict(set)

    brands = client.brands()
    LOGGER.info("Brands fetched: %s", len(brands))

    for bi, brand in enumerate(brands, start=1):
        brand_id = int(brand["value"])
        brand_label = _normalize_label(str(brand.get("text") or ""))
        if not brand_label:
            LOGGER.warning("Skip brand id=%s (empty label)", brand_id)
            done_brand_ids.add(brand_id)
            continue

        if brand_id in done_brand_ids:
            LOGGER.info(
                "[%s/%s] %s — already done (skip)",
                bi,
                len(brands),
                brand_label,
            )
            continue

        models = client.models(brand_id)
        LOGGER.info("[%s/%s] %s models=%s", bi, len(brands), brand_label, len(models))

        for model in models:
            model_id = int(model["value"])
            model_label = _normalize_label(str(model.get("text") or ""))
            if not model_label:
                continue

            try:
                years = client.years(brand_id, model_id)
            except (HTTPError, Timeout, RequestsConnectionError) as exc:
                LOGGER.error(
                    "Giving up on years brand=%s model=%s id=%s/%s: %s — skip model",
                    brand_label,
                    model_label,
                    brand_id,
                    model_id,
                    exc,
                )
                continue

            if not years:
                continue

            for jy in years:
                try:
                    trims = client.trims(brand_id, model_id, jy)
                except (HTTPError, Timeout, RequestsConnectionError) as exc:
                    LOGGER.error(
                        "Giving up on trims brand=%s model=%s year=%s: %s — skip year",
                        brand_label,
                        model_label,
                        jy,
                        exc,
                    )
                    continue

                if not trims:
                    # Brand+model only — no trim variants for this year.
                    trim_jalali_years[(brand_label, model_label, "")].add(jy)
                    continue
                for tr in trims:
                    trim_label = _normalize_label(str(tr.get("text") or ""))
                    if not trim_label:
                        trim_label = _normalize_label(str(tr.get("name") or ""))
                    # Keep empty trim labels (brand+model only).
                    trim_jalali_years[(brand_label, model_label, trim_label)].add(jy)

        done_brand_ids.add(brand_id)
        if checkpoint_path:
            _save_checkpoint(checkpoint_path, done_brand_ids, trim_jalali_years)
            LOGGER.debug("Checkpoint saved after brand %s", brand_label)

    return trim_jalali_years


def build_records(trim_jalali_years: dict[tuple[str, str, str], set[int]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for (brand, model, trim_label), jyears in sorted(trim_jalali_years.items()):
        if not jyears:
            continue
        j_min, j_max = min(jyears), max(jyears)
        gy_start, gy_end = _jalali_bounds_to_gregorian_year_span(j_min, j_max)
        records.append(
            {
                "brand": brand,
                "model": model,
                "trim": trim_label,
                "jalali_year_start": j_min,
                "jalali_year_end": j_max,
                "production_year_start": gy_start,
                "production_year_end": gy_end,
                "source_url": "https://bama.ir/calculator",
            }
        )
    return records


def write_json(records: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "https://bama.ir/calculator",
        "count": len(records),
        "empty_trim_count": sum(1 for r in records if not r["trim"]),
        "items": records,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Wrote %s records to %s", len(records), output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.12, help="Sleep between HTTP calls (seconds).")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSON output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT,
        help=f"Checkpoint path for resume (default: {DEFAULT_CHECKPOINT})",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore existing checkpoint and crawl from scratch.",
    )
    parser.add_argument("--max-retries", type=int, default=8, help="Retries for transient HTTP/network errors.")
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-request timeout (seconds).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    _configure_logging(args.verbose)

    session = requests.Session()
    session.verify = True

    client = BamaCadClient(
        session=session,
        delay_sec=args.delay,
        max_retries=args.max_retries,
        request_timeout=args.timeout,
    )
    trim_map = crawl_tree(
        client,
        checkpoint_path=args.checkpoint,
        resume=not args.no_resume,
    )
    records = build_records(trim_map)
    write_json(records, args.output)

    brands_n = len({r["brand"] for r in records})
    models_n = len({(r["brand"], r["model"]) for r in records})
    empty_trim_n = sum(1 for r in records if not r["trim"])

    print(
        f"Summary — brands: {brands_n}, "
        f"models (brand+model): {models_n}, "
        f"rows: {len(records)}, "
        f"empty trim: {empty_trim_n}, "
        f"output: {args.output}"
    )


if __name__ == "__main__":
    main()
