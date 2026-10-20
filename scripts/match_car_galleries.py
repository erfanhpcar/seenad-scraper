#!/usr/bin/env python3
"""
Match crawled OEM gallery records to SeenAd car_entities CSV.

Usage:
  python scripts/match_car_galleries.py
  python scripts/match_car_galleries.py \\
      --galleries data/raw_car_galleries.jsonl \\
      --entities tmp/car_entities_1111_202608151302.csv -v
  docker exec -i seenad_backend python3 scripts/match_car_galleries.py -v

Output:
  data/car_gallery_entity_matches.json
  data/car_gallery_entity_matches.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^0-9a-zA-Zآ-ی]+")

DEFAULT_GALLERIES = Path("data/raw_car_galleries.jsonl")
DEFAULT_ENTITIES = Path("tmp/car_entities_1111_202608151302.csv")
DEFAULT_JSON = Path("data/car_gallery_entity_matches.json")
DEFAULT_CSV = Path("data/car_gallery_entity_matches.csv")

# High-confidence (source, slug/name needle) → entity brand + model contains
ALIASES: list[tuple[str, str, str, str]] = [
    ("kermanmotor", "sr6", "کی ام سی", "SR6"),
    ("kermanmotor", "sr3", "کی ام سی", "SR3"),
    ("kermanmotor", "x5", "کی ام سی", "X5"),
    ("kermanmotor", "j7", "کی ام سی", "J7"),
    ("kermanmotor", "k7", "کی ام سی", "K7"),
    ("kermanmotor", "t8", "کی ام سی", "T8"),
    ("kermanmotor", "t9", "کی ام سی", "T9"),
    ("kermanmotor", "a5", "کی ام سی", "A5"),
    ("kermanmotor", "eagle", "کی ام سی", "ایگل"),
    ("kermanmotor", "ej7", "کی ام سی", "eJ7"),
    ("kermanmotor", "s3", "جک", "S3"),
    ("kermanmotor", "s5", "جک", "S5"),
    ("kermanmotor", "j3", "جک", "جی 3"),
    ("kermanmotor", "j4", "جک", "جی 4"),
    ("kermanmotor", "j5", "جک", "جی 5"),
    ("mvm", "x77", "ام وی ام", "X77"),
    ("mvm", "x55pro", "ام وی ام", "X55 PRO"),
    ("mvm", "x55", "ام وی ام", "X55"),
    ("mvm", "x33cross", "ام وی ام", "X33 کراس"),
    ("mvm", "a5s", "ام وی ام", "آریزو"),
    ("mvm", "m5", "ام وی ام", "M5"),
    ("lamari", "eama-specs", "لاماری", "ایما"),
    ("lamari", "eama-hev", "لاماری", "ایما HEV"),
    ("lamari", "echo", "لاماری", "اکو"),
    ("lamari", "neo", "لاماری", "نئو"),
    ("mammut", "id-4", "فولکس", "ID.4"),
    ("mammut", "tayron", "فولکس", "تایرون"),
    ("mammut", "tiguan-l-pro", "فولکس", "تیگوان L"),
    ("mammut", "tiguan", "فولکس", "تیگوان"),
    ("mammut", "t-roc", "فولکس", "تی راک"),
    ("saipa", "saina-g", "ساینا", "G دنده ای"),
    ("saipa", "saina-s", "ساینا", "S"),
    ("saipa", "shahin", "شاهین", ""),
    ("saipa", "quik", "کوییک", ""),
    ("saipa", "atlas", "اطلس", ""),
    ("saipa", "sahand", "سهند", ""),
    ("saipa", "arya", "آریا", ""),
    ("saipa", "pars-noa", "پارس نوآ", ""),
    ("fownix", "f8-pro-plugin", "فونیکس", "تیگو 8 پرو هیبرید"),
    ("fownix", "f7-pro-plugin", "فونیکس", "تیگو 7 پرو هیبرید"),
    ("fownix", "f-8-promax", "فونیکس", "تیگو 8 پرو مکس"),
    ("fownix", "f-7-promax", "فونیکس", "تیگو 7 پرو مکس"),
    ("fownix", "fx-ev", "فونیکس", "FX EV"),
    ("fownix", "fx", "فونیکس", "FX"),
    ("fownix", "z8", "فونیکس", "آریزو 8"),
    ("fownix", "z6", "فونیکس", "آریزو 6"),
    ("bahman", "فیدلیتی الیت", "فیدلیتی", "الیت"),
    ("bahman", "فیدلیتی پرستیژ", "فیدلیتی", "پرستیژ"),
    ("bahman", "فیدلیتی", "فیدلیتی", ""),
    ("bahman", "دیگنیتی پرستیژ", "دیگنیتی", "پرستیژ"),
    ("bahman", "دیگنیتی پرایم", "دیگنیتی", "پرایم"),
    ("bahman", "ریسپکت", "ریسپکت", "پرایم"),
    ("bahman", "شوال", "شوال", "6HEV"),
    ("bahman", "اینوی", "اینوی", "هیبرید"),
    ("bahman", "اینرودز", "اینرودز", "C35"),
    ("bahman", "کاپرا", "کاپرا", ""),
    ("bahman", "g9", "زد ایکس اتو", "G9"),
    ("saipa", "padra-plus", "زامیاد", "پادرا پلاس"),
    ("saipa", "karoon", "زامیاد", "کارون"),
    ("saipa", "441p", "چانگان", "441P"),
    ("saipa", "421p", "چانگان", "421P"),
    ("saipa", "c3-xr", "سیتروئن", "C3 XR"),
    ("saipa", "zamyad-ex", "زامیاد", "اکستند EX"),
    ("saipa", "zamyadvanetz24", "زامیاد", "ون کاروان"),
    ("saipa", "saipa151", "زامیاد", "وانت"),
    ("mammut", "golf-gti", "فولکس", "گلف"),
    ("ikco", "لونا", "لونا", "GRE"),
    ("ikco", "ری را", "ری را", ""),
    ("ikco", "گرندویتارا", "سوزوکی", "گراند ویتارا (مونتاژ)"),
    ("ikco", "ویتارا", "سوزوکی", "گراند ویتارا (مونتاژ)"),
    ("ikco", "تارا", "تارا", ""),
    ("ikco", "تندر پیکاپ", "رنو", "تندر وانت"),
    ("ikco", "تندر", "رنو", "تندر 90"),
    ("ikco", "کپچر", "رنو", "کپچر"),
    ("ikco", "شاین مکس", "دانگ فنگ", "شاین مکس"),
    ("ikco", "h30", "دانگ فنگ", "H30"),
    ("ikco", "s30", "دانگ فنگ", "S30"),
    ("ikco", "e70", "دانگ فنگ", "E70"),
    ("ikco", "بستیون nat", "بستیون", "نات"),
    ("ikco", "دنا پلاس", "دنا", "پلاس"),
    ("ikco", "دنا", "دنا", "معمولی"),
    ("ikco", "رانا پلاس", "رانا", "پلاس"),
    ("ikco", "رانا", "رانا", ""),
    ("ikco", "سمند se", "سمند", "SE"),
    ("ikco", "سمند lx", "سمند", "LX"),
    ("ikco", "سمند", "سمند", ""),
    ("ikco", "سورن", "سمند", "سورن"),
    ("ikco", "آریسان", "آریسان", ""),
    ("ikco", "هایما 7x", "هایما", "7X"),
    ("ikco", "هایما 8s", "هایما", "8S"),
    ("ikco", "هایما s7", "هایما", "S7"),
    ("ikco", "هایما s5", "هایما", "S5"),
    ("ikco", "هایما", "هایما", ""),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip().lower()
    return text


def compact(value: str | None) -> str:
    return NON_ALNUM_RE.sub("", norm(value))


def load_entities(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["_brand_n"] = norm(row.get("brand"))
        row["_model_n"] = norm(row.get("model"))
        row["_trim_n"] = norm(row.get("trim"))
        row["_brand_c"] = compact(row.get("brand"))
        row["_model_c"] = compact(row.get("model"))
        row["_hay"] = " ".join(
            x for x in (row["_brand_n"], row["_model_n"], row["_trim_n"], norm(row.get("slug"))) if x
        )
    return rows


def load_galleries(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def needle_hits(needle: str, hay: str) -> bool:
    n = norm(needle)
    h = norm(hay)
    if not n or not h:
        return False
    if n == h:
        return True
    tokens = [t for t in re.split(r"[\s/_-]+", h) if t]
    if n in tokens:
        return True
    cn = compact(n)
    if not cn:
        return False
    if cn == compact(h):
        return True
    compact_tokens = [compact(t) for t in tokens]
    if cn in compact_tokens:
        return True
    n_tokens = [compact(t) for t in re.split(r"[\s/_-]+", n) if t]
    if len(n_tokens) >= 2 and all(t in compact_tokens for t in n_tokens):
        return True
    return False


def model_lock(entity: dict[str, Any], model: str) -> bool:
    model_n = norm(model)
    model_c = compact(model)
    em = entity["_model_n"]
    ec = entity["_model_c"]
    if not model_n:
        return True
    if em == model_n or ec == model_c:
        return True
    if em.startswith(model_n + " "):
        return True
    tokens = [t for t in re.split(r"[\s/_-]+", em) if t]
    compact_tokens = [compact(t) for t in tokens]
    if model_n in tokens or (len(model_c) >= 2 and model_c in compact_tokens):
        return True
    if len(model_c) >= 4 and (model_n in em or model_c in ec):
        return True
    return False


def alias_candidates(record: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    slug = norm(record.get("slug"))
    name = norm(record.get("name"))
    source = record.get("source")
    hay = f"{slug} {name} {norm(record.get('model'))}"
    for src, needle, brand, model in ALIASES:
        if src != source:
            continue
        if not needle_hits(needle, hay):
            continue
        brand_n = norm(brand)
        picked = [e for e in entities if e["_brand_n"] == brand_n]
        if model:
            tighter = [e for e in picked if model_lock(e, model)]
            if not tighter:
                continue
            picked = tighter
        if picked:
            return picked
    return None


def score_entity(record: dict[str, Any], entity: dict[str, Any]) -> float:
    brand = norm(record.get("brand"))
    model = norm(record.get("model"))
    name = norm(record.get("name"))
    slug = norm(record.get("slug"))
    query = " ".join(x for x in (brand, model, name, slug) if x)
    brand_score = fuzz.ratio(brand, entity["_brand_n"]) if brand else 0
    model_score = max(
        fuzz.token_set_ratio(model or name, entity["_model_n"]),
        fuzz.partial_ratio(model or name, entity["_model_n"]),
        fuzz.token_set_ratio(slug, entity["_model_n"]),
    )
    hay_score = fuzz.token_set_ratio(query, entity["_hay"])
    score = 0.35 * brand_score + 0.45 * model_score + 0.20 * hay_score
    if brand and brand == entity["_brand_n"]:
        score += 8
    return float(score)


COMMERCIAL_RE = re.compile(
    r"امپاور|فورس|کامیون|کمپرسی|تانکر|آتش نشانی|کاربری|اتوبوس|مینی بوس|کشنده|موتورسیکلت"
)
LATIN_NAME_RE = re.compile(r"^[A-Za-z0-9(][A-Za-z0-9 \-()/.]*$")
FORCE_UNMATCHED_RE = (
    ("bahman", re.compile(r"xb1", re.I)),
    ("mammut", re.compile(r"vs7", re.I)),
    ("mvm", re.compile(r"(^|[\s/_-])x5($|[\s/_-])", re.I)),
)


def is_out_of_scope(record: dict[str, Any]) -> bool:
    name = record.get("name") or ""
    if COMMERCIAL_RE.search(name):
        return True
    if record.get("source") == "bahman" and re.search(r"تریل", name):
        return True
    # Bahman motorcycle pages are latin model codes, not passenger cars.
    if record.get("source") == "bahman" and LATIN_NAME_RE.fullmatch(name.strip()):
        if re.search(r"\bg9\b", name, re.I):
            return False
        return True
    return False


def is_force_unmatched(record: dict[str, Any]) -> bool:
    hay = f"{record.get('slug') or ''} {record.get('name') or ''}"
    source = record.get("source")
    return any(src == source and pat.search(hay) for src, pat in FORCE_UNMATCHED_RE)


def match_record(record: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
    name = record.get("name") or ""
    if is_out_of_scope(record):
        return {
            "source": record.get("source"),
            "name": record.get("name"),
            "brand_guess": record.get("brand"),
            "model_guess": record.get("model"),
            "slug": record.get("slug"),
            "source_url": record.get("source_url"),
            "image_count": record.get("image_count") or 0,
            "status": "out_of_scope",
            "matches": [],
            "top_candidates": [],
        }
    if is_force_unmatched(record):
        return {
            "source": record.get("source"),
            "name": record.get("name"),
            "brand_guess": record.get("brand"),
            "model_guess": record.get("model"),
            "slug": record.get("slug"),
            "source_url": record.get("source_url"),
            "image_count": record.get("image_count") or 0,
            "status": "unmatched",
            "matches": [],
            "top_candidates": [],
        }
    aliased = alias_candidates(record, entities)
    pool = aliased if aliased is not None else entities
    if record.get("brand"):
        brand_n = norm(record.get("brand"))
        branded = [e for e in pool if e["_brand_n"] == brand_n]
        if branded:
            pool = branded
    ranked = sorted(((score_entity(record, e), e) for e in pool), key=lambda x: x[0], reverse=True)
    top = ranked[:8] if ranked else []
    best_score = top[0][0] if top else 0.0
    # Keep all trims of the winning model when brand+model lock is strong
    matches: list[dict[str, Any]] = []
    if aliased is not None and aliased:
        for entity in aliased:
            matches.append(
                {
                    "entity_id": int(entity["id"]),
                    "brand": entity["brand"],
                    "model": entity["model"],
                    "trim": entity.get("trim") or "",
                    "slug": entity.get("slug"),
                    "score": round(best_score, 1),
                    "method": "alias",
                }
            )
        status = "matched"
    elif best_score >= 78 and top:
        winner = top[0][1]
        same_model = [
            e
            for _, e in ranked
            if e["_brand_n"] == winner["_brand_n"] and e["_model_n"] == winner["_model_n"]
        ]
        for entity in same_model:
            matches.append(
                {
                    "entity_id": int(entity["id"]),
                    "brand": entity["brand"],
                    "model": entity["model"],
                    "trim": entity.get("trim") or "",
                    "slug": entity.get("slug"),
                    "score": round(best_score, 1),
                    "method": "fuzzy",
                }
            )
        status = "matched"
    elif best_score >= 60 and top:
        entity = top[0][1]
        matches.append(
            {
                "entity_id": int(entity["id"]),
                "brand": entity["brand"],
                "model": entity["model"],
                "trim": entity.get("trim") or "",
                "slug": entity.get("slug"),
                "score": round(best_score, 1),
                "method": "review",
            }
        )
        status = "needs_review"
    else:
        status = "unmatched"

    return {
        "source": record.get("source"),
        "name": record.get("name"),
        "brand_guess": record.get("brand"),
        "model_guess": record.get("model"),
        "slug": record.get("slug"),
        "source_url": record.get("source_url"),
        "image_count": record.get("image_count") or 0,
        "status": status,
        "matches": matches,
        "top_candidates": [
            {
                "entity_id": int(e["id"]),
                "brand": e["brand"],
                "model": e["model"],
                "trim": e.get("trim") or "",
                "score": round(sc, 1),
            }
            for sc, e in top[:3]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Match OEM galleries to car entities")
    parser.add_argument("--galleries", default=str(DEFAULT_GALLERIES))
    parser.add_argument("--entities", default=str(DEFAULT_ENTITIES))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_CSV))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    galleries_path = Path(args.galleries)
    entities_path = Path(args.entities)
    if not galleries_path.exists():
        print(f"missing galleries: {galleries_path}", file=sys.stderr)
        return 1
    if not entities_path.exists():
        print(f"missing entities: {entities_path}", file=sys.stderr)
        return 1

    entities = load_entities(entities_path)
    galleries = load_galleries(galleries_path)
    results = [match_record(row, entities) for row in galleries]

    counts: dict[str, int] = defaultdict(int)
    matched_entity_ids: set[int] = set()
    for row in results:
        counts[str(row["status"])] += 1
        for match in row["matches"]:
            matched_entity_ids.add(match["entity_id"])

    payload = {
        "matched_at": now_iso(),
        "galleries": str(galleries_path),
        "entities": str(entities_path),
        "gallery_count": len(galleries),
        "entity_count": len(entities),
        "status_counts": dict(counts),
        "matched_entity_count": len(matched_entity_ids),
        "results": results,
    }
    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out_csv = Path(args.output_csv)
    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "status",
                "source",
                "source_name",
                "source_url",
                "image_count",
                "entity_id",
                "brand",
                "model",
                "trim",
                "score",
                "method",
            ],
        )
        writer.writeheader()
        for row in results:
            if row["matches"]:
                for match in row["matches"]:
                    writer.writerow(
                        {
                            "status": row["status"],
                            "source": row["source"],
                            "source_name": row["name"],
                            "source_url": row["source_url"],
                            "image_count": row["image_count"],
                            "entity_id": match["entity_id"],
                            "brand": match["brand"],
                            "model": match["model"],
                            "trim": match["trim"],
                            "score": match["score"],
                            "method": match["method"],
                        }
                    )
            else:
                writer.writerow(
                    {
                        "status": row["status"],
                        "source": row["source"],
                        "source_name": row["name"],
                        "source_url": row["source_url"],
                        "image_count": row["image_count"],
                        "entity_id": "",
                        "brand": row.get("brand_guess") or "",
                        "model": row.get("model_guess") or "",
                        "trim": "",
                        "score": "",
                        "method": "",
                    }
                )

    print(
        f"galleries={len(galleries)} matched={counts.get('matched', 0)} "
        f"review={counts.get('needs_review', 0)} unmatched={counts.get('unmatched', 0)} "
        f"entities_covered={len(matched_entity_ids)}/{len(entities)}"
    )
    print(f"Wrote {out_json}")
    print(f"Wrote {out_csv}")
    if args.verbose:
        by_source: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in results:
            by_source[str(row["source"])][str(row["status"])] += 1
        for source, st in sorted(by_source.items()):
            print(f"  {source}: {dict(st)}")
        print("unmatched sample:")
        for row in results:
            if row["status"] == "unmatched":
                print(f"  - {row['source']} {row['name']} ({row.get('brand_guess')}/{row.get('model_guess')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
