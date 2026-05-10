"""
Bama.ir zero-price (catalogue day price) ingestion via official CAD JSON APIs.

Feeds used (same as the Nuxt frontend on https://bama.ir/price):

- ``GET /cad/api/price/hierarchy?pageIndex=&pageSize=`` — grouped trims with IRR prices.

Rows are matched to ``car_entities`` with RapidFuzz (fuzzy string scores are logged).

A row is persisted only if ``min(brand_score, model_score, trim_score) >= 75``.

Persist one row per ``car_reference_id`` in ``car_zero_price`` (upsert).

"""

from __future__ import annotations

import json
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Iterator, Mapping, Sequence

import requests
from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import CarEntity, CarZeroPrice

LOGGER: Final = logging.getLogger(__name__)

BAMA_BASE_URL: Final[str] = "https://bama.ir"
HIERARCHY_PATH: Final[str] = "/cad/api/price/hierarchy"
DEFAULT_PAGE_SIZE: Final[int] = 75
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 45.0
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PERSIAN_DIGITS: Final[str] = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

FUZZ_BRAND_CUTOFF: Final[int] = 82
FUZZ_MODEL_CUTOFF: Final[int] = 80
FUZZ_TRIM_CUTOFF: Final[int] = 78

# Minimum of (brand_score, model_score, trim_score) required to persist a row.
MIN_COMBINED_MATCH_SCORE: Final[int] = 75

RAW_SNAPSHOT_MAX_JSON_LEN: Final[int] = 8000


@dataclass(frozen=True, slots=True)
class BamaHierarchyItem:
    """One trim line from the hierarchy API ``items`` array."""

    brand_slug: str
    brand_fa: str
    model_slug: str
    model_fa: str
    trim_slug: str
    trim_fa: str
    price_irr: int
    model_year: int | None
    price_provider: str | None
    bama_car_price_id: int | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MatchDecision:
    car_reference_id: int
    matched_brand: str
    matched_model: str
    matched_trim: str
    brand_score: int
    model_score: int
    trim_score: int


@dataclass(frozen=True, slots=True)
class BamaZeroPriceRunSummary:
    pages_fetched: int
    items_seen: int
    items_with_price: int
    matched: int
    upserted: int
    skipped_no_entity: int
    rejected_low_score: int
    errors_http: int

    def as_celery_dict(self) -> dict[str, Any]:
        return {
            "pages_fetched": self.pages_fetched,
            "items_seen": self.items_seen,
            "items_with_price": self.items_with_price,
            "matched": self.matched,
            "upserted": self.upserted,
            "skipped_no_entity": self.skipped_no_entity,
            "rejected_low_score": self.rejected_low_score,
            "errors_http": self.errors_http,
        }


def slug_phrase(slug: str) -> str:
    """Human-ish token sequence from API slug for fuzzy queries."""

    return slug.strip().replace("-", " ").replace("_", " ").strip()


def build_bilingual_query(fa_label: str, slug: str) -> str:
    parts = [fa_label.strip(), slug_phrase(slug)]
    return " ".join(p for p in parts if p).strip()


def normalize_compare_key(text: str) -> str:
    return text.translate(PERSIAN_DIGITS).casefold().strip()


def clipped_json_snapshot(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        blob = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return None
    if len(blob) > RAW_SNAPSHOT_MAX_JSON_LEN:
        return {"truncated": True, "json_prefix": blob[: RAW_SNAPSHOT_MAX_JSON_LEN]}
    return dict(raw)


class CarEntityMatcher:
    """
    Three-step fuzzy resolution: brand → model → trim over ``CarEntity`` rows.

    Logs winning RapidFuzz ``WRatio`` scores at INFO for traceability.
    """

    def __init__(
        self,
        entities: Sequence[CarEntity],
        *,
        brand_cutoff: int = FUZZ_BRAND_CUTOFF,
        model_cutoff: int = FUZZ_MODEL_CUTOFF,
        trim_cutoff: int = FUZZ_TRIM_CUTOFF,
    ) -> None:
        self._brand_cutoff = brand_cutoff
        self._model_cutoff = model_cutoff
        self._trim_cutoff = trim_cutoff
        self._entities: tuple[CarEntity, ...] = tuple(entities)

        self._brand_choices = sorted({e.brand.strip() for e in self._entities if e.brand.strip()})

        models_by_brand: dict[str, set[str]] = defaultdict(set)
        by_bm: dict[tuple[str, str], list[CarEntity]] = defaultdict(list)
        for e in self._entities:
            brand = e.brand.strip()
            model = e.model.strip()
            if not brand or not model:
                continue
            models_by_brand[brand].add(model)
            by_bm[(brand, model)].append(e)

        self._models_by_brand: dict[str, tuple[str, ...]] = {
            brand: tuple(sorted(models)) for brand, models in models_by_brand.items()
        }
        self._by_brand_model: dict[tuple[str, str], tuple[CarEntity, ...]] = {
            kb: tuple(vs) for kb, vs in by_bm.items()
        }

    def _resolve_brand(self, row: BamaHierarchyItem) -> tuple[str, int]:
        slug_only = slug_phrase(row.brand_slug)
        bilingual = build_bilingual_query(row.brand_fa, row.brand_slug)
        slug_key = normalize_compare_key(slug_only)
        fa_key = normalize_compare_key(row.brand_fa)

        exact_brand = next((b for b in self._brand_choices if normalize_compare_key(b) == fa_key), None)
        if exact_brand:
            LOGGER.info(
                "bama_price match brand via exact FA key: api=%s / %s -> entity_brand=%r score=%s",
                row.brand_fa,
                row.brand_slug,
                exact_brand,
                100,
            )
            return exact_brand, 100

        if slug_key:
            exact_slug = next(
                (
                    b
                    for b in self._brand_choices
                    if normalize_compare_key(b) == slug_key or normalize_compare_key(slug_phrase(b)) == slug_key
                ),
                None,
            )
            if exact_slug:
                LOGGER.info(
                    "bama_price match brand via exact slug key: api=%s -> entity_brand=%r score=%s",
                    row.brand_slug,
                    exact_slug,
                    100,
                )
                return exact_slug, 100

        best: tuple[str, int] | None = None
        for query_str in dict.fromkeys((bilingual, slug_only, row.brand_fa.strip())):
            if not query_str:
                continue
            hit = process.extractOne(
                query_str,
                self._brand_choices,
                scorer=fuzz.WRatio,
                score_cutoff=self._brand_cutoff,
            )
            if hit is None:
                continue
            candidate, score, _idx = hit
            if best is None or score > best[1]:
                best = (candidate, int(score))

        if best:
            LOGGER.info(
                "bama_price match brand via fuzzy WRatio: query=%r api=%s/%s -> entity_brand=%r score=%s",
                bilingual,
                row.brand_fa,
                row.brand_slug,
                best[0],
                best[1],
            )
            return best[0], best[1]

        dbg = process.extractOne(
            bilingual or row.brand_fa,
            self._brand_choices,
            scorer=fuzz.WRatio,
        )
        LOGGER.warning(
            "bama_price brand unresolved: api=%s / %s best=%s (threshold=%s)",
            row.brand_fa,
            row.brand_slug,
            dbg,
            self._brand_cutoff,
        )
        raise LookupError("brand")

    def _resolve_model(self, brand_canon: str, row: BamaHierarchyItem) -> tuple[str, int]:
        model_choices = self._models_by_brand.get(brand_canon)
        if not model_choices:
            LOGGER.warning(
                "bama_price model skipped; no entities for matched brand=%r slug=%s",
                brand_canon,
                row.brand_slug,
            )
            raise LookupError("model")

        bilingual = build_bilingual_query(row.model_fa, row.model_slug)
        slug_only = slug_phrase(row.model_slug)
        fa_key = normalize_compare_key(row.model_fa)
        slug_key = normalize_compare_key(slug_only)

        exact_fa = next((m for m in model_choices if normalize_compare_key(m) == fa_key), None)
        if exact_fa:
            LOGGER.info(
                "bama_price match model via exact FA: brand=%r api=%s -> entity_model=%r score=%s",
                brand_canon,
                row.model_fa,
                exact_fa,
                100,
            )
            return exact_fa, 100

        if slug_key:
            exact_slug = next((m for m in model_choices if normalize_compare_key(m) == slug_key), None)
            if exact_slug:
                LOGGER.info(
                    "bama_price match model via slug key containment: brand=%r api=%s -> entity_model=%r score=%s",
                    brand_canon,
                    row.model_slug,
                    exact_slug,
                    100,
                )
                return exact_slug, 100

        best: tuple[str, int] | None = None
        for query_str in dict.fromkeys((bilingual, slug_only, row.model_fa.strip())):
            if not query_str:
                continue
            hit = process.extractOne(
                query_str,
                model_choices,
                scorer=fuzz.WRatio,
                score_cutoff=self._model_cutoff,
            )
            if hit is None:
                continue
            candidate, score, _idx = hit
            if best is None or score > best[1]:
                best = (candidate, int(score))

        if best:
            LOGGER.info(
                "bama_price match model fuzzy: brand=%r query=%r api=%s/%s -> entity_model=%r score=%s",
                brand_canon,
                bilingual,
                row.model_fa,
                row.model_slug,
                best[0],
                best[1],
            )
            return best[0], best[1]

        dbg = process.extractOne(
            bilingual or row.model_fa,
            model_choices,
            scorer=fuzz.WRatio,
        )
        LOGGER.warning(
            "bama_price model unresolved: brand=%r api=%s / %s best=%s threshold=%s",
            brand_canon,
            row.model_fa,
            row.model_slug,
            dbg,
            self._model_cutoff,
        )
        raise LookupError("model")

    def _pick_trim_entity(self, brand_canon: str, model_canon: str, row: BamaHierarchyItem) -> tuple[CarEntity, int]:
        rows = self._by_brand_model.get((brand_canon, model_canon), ())
        if not rows:
            LOGGER.warning(
                "bama_price trim skipped; bucket empty brand=%r model=%r slug=%s",
                brand_canon,
                model_canon,
                row.model_slug,
            )
            raise LookupError("trim")

        trim_targets = [(e, e.trim.strip() if e.trim.strip() else "پایه") for e in rows]
        choice_labels = [t[1] for t in trim_targets]

        bilingual = build_bilingual_query(row.trim_fa, row.trim_slug)
        slug_only = slug_phrase(row.trim_slug)
        fa_key = normalize_compare_key(row.trim_fa)
        slug_key = normalize_compare_key(slug_only)

        for entity, trimmed in trim_targets:
            if normalize_compare_key(trimmed) == fa_key:
                LOGGER.info(
                    "bama_price match trim exact FA: ids=%s brand=%r model=%r entity_trim=%r score=%s",
                    entity.id,
                    brand_canon,
                    model_canon,
                    trimmed,
                    100,
                )
                return entity, 100

        for entity, trimmed in trim_targets:
            tk = normalize_compare_key(trimmed)
            if tk == slug_key or (bool(slug_key) and slug_key in tk):
                LOGGER.info(
                    "bama_price match trim via slug heuristic: ids=%s brand=%r model=%r trim=%r score=%s",
                    entity.id,
                    brand_canon,
                    model_canon,
                    trimmed,
                    98,
                )
                return entity, 98

        best: tuple[CarEntity, int] | None = None
        for query_str in dict.fromkeys((bilingual, slug_only, row.trim_fa.strip())):
            if not query_str:
                continue
            hit = process.extractOne(
                query_str,
                choice_labels,
                scorer=fuzz.WRatio,
                score_cutoff=self._trim_cutoff,
            )
            if hit is None:
                continue
            candidate_label, score, idx = hit
            entity_at = trim_targets[int(idx)][0]
            if best is None or int(score) > best[1]:
                best = (entity_at, int(score))

        if best:
            ent, scr = best
            LOGGER.info(
                "bama_price match trim fuzzy: id=%s brand=%r model=%r api=%s/%s -> entity_trim=%r score=%s",
                ent.id,
                brand_canon,
                model_canon,
                row.trim_fa,
                row.trim_slug,
                ent.trim.strip() or "پایه",
                scr,
            )
            return ent, scr

        dbg = process.extractOne(
            bilingual or row.trim_fa,
            choice_labels,
            scorer=fuzz.WRatio,
        )
        LOGGER.warning(
            "bama_price trim unresolved: brand=%r model=%r api=%s/%s best=%s threshold=%s",
            brand_canon,
            model_canon,
            row.trim_fa,
            row.trim_slug,
            dbg,
            self._trim_cutoff,
        )
        raise LookupError("trim")

    def match(self, row: BamaHierarchyItem) -> MatchDecision | None:
        try:
            brand_canon, b_score = self._resolve_brand(row)
            model_canon, m_score = self._resolve_model(brand_canon, row)
            entity, t_score = self._pick_trim_entity(brand_canon, model_canon, row)
        except LookupError:
            return None

        trim_label = entity.trim.strip() or "پایه"
        return MatchDecision(
            car_reference_id=int(entity.id),
            matched_brand=brand_canon,
            matched_model=model_canon,
            matched_trim=trim_label,
            brand_score=b_score,
            model_score=m_score,
            trim_score=t_score,
        )


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
        }
    )
    return session


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _parse_hierarchy_item(brand_bundle: Mapping[str, Any], item: Mapping[str, Any]) -> BamaHierarchyItem:
    return BamaHierarchyItem(
        brand_slug=str(item.get("brand") or brand_bundle.get("brand") or ""),
        brand_fa=str(item.get("brand_fa") or brand_bundle.get("brand_fa") or ""),
        model_slug=str(item.get("model") or ""),
        model_fa=str(item.get("model_fa") or ""),
        trim_slug=str(item.get("trim") or ""),
        trim_fa=str(item.get("trim_fa") or ""),
        price_irr=int(item.get("price") or 0),
        model_year=_optional_int(item.get("model_year")),
        price_provider=item.get("price_provider") if item.get("price_provider") else None,
        bama_car_price_id=_optional_int(item.get("car_price_id")),
        raw=item,
    )


def iterate_hierarchy_items(
    session: requests.Session,
    *,
    base_url: str = BAMA_BASE_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_S,
    inter_page_delay_seconds: tuple[float, float] = (0.4, 1.2),
) -> Iterator[tuple[int, BamaHierarchyItem]]:
    """Yield ``(page_index, item)`` across all hierarchy pages (for bookkeeping)."""

    url = base_url.rstrip("/") + HIERARCHY_PATH
    page_index = 0
    while True:
        time.sleep(random.uniform(*inter_page_delay_seconds))
        response = session.get(
            url,
            params={"pageIndex": page_index, "pageSize": page_size},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload: Mapping[str, Any] = response.json()
        blocks = payload.get("data") or []
        LOGGER.info(
            "bama_price hierarchy page=%s size=%s groups=%s",
            page_index,
            page_size,
            len(blocks),
        )

        if not blocks:
            return

        for group in blocks:
            if not isinstance(group, Mapping):
                continue
            for raw_item in group.get("items") or []:
                if not isinstance(raw_item, Mapping):
                    continue
                yield page_index, _parse_hierarchy_item(group, raw_item)

        if len(blocks) < page_size:
            return

        page_index += 1


def upsert_car_zero_price(
    db: Session,
    *,
    car_reference_id: int,
    price_irr: int,
    model_year: int | None,
    price_provider: str | None,
    bama_car_price_id: int | None,
    raw_snapshot: Mapping[str, Any] | None,
) -> None:
    price_decimal = Decimal(price_irr)
    snap = clipped_json_snapshot(raw_snapshot) if raw_snapshot is not None else None

    stmt = pg_insert(CarZeroPrice).values(
        car_reference_id=car_reference_id,
        price=price_decimal,
        currency="IRR",
        model_year=model_year,
        price_provider=price_provider,
        bama_car_price_id=bama_car_price_id,
        source="bama.ir/price",
        raw_snapshot=snap,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[CarZeroPrice.car_reference_id],
        set_={
            "price": stmt.excluded.price,
            "model_year": stmt.excluded.model_year,
            "price_provider": stmt.excluded.price_provider,
            "bama_car_price_id": stmt.excluded.bama_car_price_id,
            "raw_snapshot": stmt.excluded.raw_snapshot,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)


def load_car_entities(db: Session) -> list[CarEntity]:
    return list(db.scalars(select(CarEntity)).all())


def run_bama_zero_price_pipeline(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    dry_run: bool = False,
) -> BamaZeroPriceRunSummary:
    """
    Download the full car hierarchy from Bama, match each row to ``car_entities``,
    and upsert ``car_zero_price`` (unless ``dry_run``).
    """

    LOGGER.info(
        "bama_price pipeline start dry_run=%s page_size=%s endpoint=%s",
        dry_run,
        page_size,
        HIERARCHY_PATH,
    )
    http = _http_session()
    items_seen = 0
    items_with_price = 0
    matched = 0
    upserted = 0
    skipped_no_entity = 0
    rejected_low_score = 0
    errors_http = 0

    page_indexes: set[int] = set()
    with SessionLocal() as db:
        matcher = CarEntityMatcher(load_car_entities(db))
        try:
            for page_idx, item in iterate_hierarchy_items(http, page_size=page_size):
                page_indexes.add(page_idx)
                items_seen += 1
                if item.price_irr <= 0:
                    LOGGER.debug(
                        "bama_price skip non-positive price car_price_id=%s %s/%s/%s",
                        item.bama_car_price_id,
                        item.brand_slug,
                        item.model_slug,
                        item.trim_slug,
                    )
                    continue
                items_with_price += 1
                decision = matcher.match(item)
                if decision is None:
                    skipped_no_entity += 1
                    continue
                min_score = min(decision.brand_score, decision.model_score, decision.trim_score)
                if min_score < MIN_COMBINED_MATCH_SCORE:
                    rejected_low_score += 1
                    LOGGER.info(
                        "bama_price reject min_score=%s < %s bama_id=%s "
                        "scores brand=%s model=%s trim=%s would_map_to_car_reference_id=%s %s/%s/%s",
                        min_score,
                        MIN_COMBINED_MATCH_SCORE,
                        item.bama_car_price_id,
                        decision.brand_score,
                        decision.model_score,
                        decision.trim_score,
                        decision.car_reference_id,
                        item.brand_slug,
                        item.model_slug,
                        item.trim_slug,
                    )
                    continue
                matched += 1
                LOGGER.debug(
                    "bama_price resolved car_reference_id=%s scores brand=%s model=%s trim=%s",
                    decision.car_reference_id,
                    decision.brand_score,
                    decision.model_score,
                    decision.trim_score,
                )
                if dry_run:
                    continue
                upsert_car_zero_price(
                    db,
                    car_reference_id=decision.car_reference_id,
                    price_irr=item.price_irr,
                    model_year=item.model_year,
                    price_provider=item.price_provider,
                    bama_car_price_id=item.bama_car_price_id,
                    raw_snapshot=item.raw,
                )
                upserted += 1
            if not dry_run:
                db.commit()
        except requests.RequestException as exc:
            errors_http += 1
            LOGGER.exception("bama_price HTTP failure: %s", exc)
            db.rollback()
            raise

    summary = BamaZeroPriceRunSummary(
        pages_fetched=len(page_indexes),
        items_seen=items_seen,
        items_with_price=items_with_price,
        matched=matched,
        upserted=upserted,
        skipped_no_entity=skipped_no_entity,
        rejected_low_score=rejected_low_score,
        errors_http=errors_http,
    )
    LOGGER.info(
        "bama_price run summary | hierarchy_rows=%s priced_rows=%s mapped_ok=%s upserted=%s "
        "rejected_score_lt_%s=%s no_entity_match=%s pages=%s",
        summary.items_seen,
        summary.items_with_price,
        summary.matched,
        summary.upserted,
        MIN_COMBINED_MATCH_SCORE,
        summary.rejected_low_score,
        summary.skipped_no_entity,
        summary.pages_fetched,
    )
    LOGGER.info("bama_price pipeline done %s", summary.as_celery_dict())
    return summary
