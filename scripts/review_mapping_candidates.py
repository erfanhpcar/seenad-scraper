from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
BRAND_ALIASES: dict[str, str] = {
    "پژو": "پژو",
    "peugeot": "پژو",
    "تیبا": "تیبا",
    "تارا": "تارا",
    "tara": "تارا",
    "پیکان": "پیکان",
    "شاهین": "شاهین",
    "کوییک": "کوییک",
    "quick": "کوییک",
    "دنا": "دنا",
    "dena": "دنا",
    "لاماری": "لاماری",
    "jac": "جک",
    "جک": "جک",
    "رنو": "رنو",
    "رانا": "رانا",
    "سیتروئن": "سیتروئن",
    "citroen": "سیتروئن",
    "اطلس": "اطلس",
    "نیسان": "نیسان",
    "ایران خودرو": "ایران خودرو",
}


@dataclass
class CarRef:
    id: int
    brand: str
    model: str
    trim: str
    full_norm: str

    @property
    def title(self) -> str:
        return f"{self.brand} | {self.model} | {self.trim}"


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS).replace("\u200c", " ").strip().lower()
    cleaned = NON_WORD_RE.sub(" ", cleaned)
    return SPACE_RE.sub(" ", cleaned).strip()


def score_similarity(a: str, b: str) -> int:
    if not a or not b:
        return 0
    return int(SequenceMatcher(None, a, b).ratio() * 100)


def load_references() -> list[CarRef]:
    query = text(
        """
        SELECT id, brand, model, trim
        FROM car_entities
        ORDER BY id
        """
    )
    with SessionLocal() as session:
        rows = session.execute(query).mappings().all()
    return [
        CarRef(
            id=int(row["id"]),
            brand=str(row["brand"] or ""),
            model=str(row["model"] or ""),
            trim=str(row["trim"] or ""),
            full_norm=normalize_text(
                f"{row['brand'] or ''} {row['model'] or ''} {row['trim'] or ''}"
            ),
        )
        for row in rows
    ]


def load_unverified_mappings(provider_name: str, limit: int | None) -> list[dict]:
    sql = """
        SELECT
            mpd.id,
            mpd.source_string,
            mpd.target_reference_id,
            mpd.match_score,
            cr.brand,
            cr.model,
            cr.trim
        FROM mapping_provider_data mpd
        LEFT JOIN car_entities cr
          ON cr.id = mpd.target_reference_id
        WHERE mpd.provider_name = :provider_name
          AND COALESCE(mpd.is_verified, FALSE) = FALSE
        ORDER BY mpd.id
    """
    params: dict[str, object] = {"provider_name": provider_name}
    if limit is not None and limit > 0:
        sql += "\nLIMIT :limit"
        params["limit"] = limit
    with SessionLocal() as session:
        return session.execute(text(sql), params).mappings().all()


def top_candidates(source_string: str, references: list[CarRef], top_n: int, exclude_id: int | None) -> list[dict]:
    source_norm = normalize_text(source_string)
    brand_norm = infer_brand(source_norm)
    candidate_refs = references
    if brand_norm:
        filtered = [ref for ref in references if normalize_text(ref.brand) == brand_norm]
        if filtered:
            candidate_refs = filtered
    scored: list[tuple[int, CarRef]] = []
    for ref in candidate_refs:
        score = score_similarity(source_norm, ref.full_norm)
        if source_norm and (source_norm in ref.full_norm or ref.full_norm in source_norm):
            score += 8
        scored.append((score, ref))
    scored.sort(key=lambda item: item[0], reverse=True)

    out: list[dict] = []
    for score, ref in scored:
        if exclude_id is not None and ref.id == exclude_id:
            continue
        out.append(
            {
                "reference_id": ref.id,
                "title": ref.title,
                "score": score,
            }
        )
        if len(out) >= top_n:
            break
    return out


def infer_brand(source_norm: str) -> str | None:
    for alias, canonical_brand in sorted(BRAND_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        alias_norm = normalize_text(alias)
        if alias_norm and alias_norm in source_norm:
            return normalize_text(canonical_brand)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Review unverified mapping rows with alternative candidates.")
    parser.add_argument("--provider", default="divar")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--alternatives", type=int, default=2)
    parser.add_argument("--format", choices=["json", "table"], default="json")
    args = parser.parse_args()

    limit = args.limit if args.limit > 0 else None
    references = load_references()
    rows = load_unverified_mappings(provider_name=args.provider, limit=limit)

    result: list[dict] = []
    for row in rows:
        current_target = None
        if row["target_reference_id"] is not None:
            current_target = {
                "reference_id": int(row["target_reference_id"]),
                "title": f"{row['brand']} | {row['model']} | {row['trim']}",
            }
        alternatives = top_candidates(
            source_string=str(row["source_string"] or ""),
            references=references,
            top_n=args.alternatives,
            exclude_id=int(row["target_reference_id"]) if row["target_reference_id"] is not None else None,
        )
        result.append(
            {
                "mapping_id": int(row["id"]),
                "source_string": str(row["source_string"] or ""),
                "score": int(row["match_score"] or 0),
                "current_target": current_target,
                "alternative_candidates": alternatives,
            }
        )

    if args.format == "table":
        print("id | source_string | score | current_target | alternatives")
        for item in result:
            current_title = item["current_target"]["title"] if item["current_target"] else "NULL"
            alternatives = ", ".join(
                [f"{cand['reference_id']}:{cand['title']} (s={cand['score']})" for cand in item["alternative_candidates"]]
            )
            print(f"{item['mapping_id']} | {item['source_string']} | {item['score']} | {current_title} | {alternatives}")
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
