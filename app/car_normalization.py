from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class NormalizedCar:
    brand_name: str | None
    model_name: str | None
    trim_name: str | None


_SPACE_RE = re.compile(r"\s+")
_PERSIAN_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.replace("\u200c", " ").strip().translate(_PERSIAN_DIGIT_TRANSLATION)
    return _SPACE_RE.sub(" ", cleaned)


def normalize_brand_model(brand_raw: str | None, tip_raw: str | None = None) -> NormalizedCar:
    text = _clean_text(f"{brand_raw or ''} {tip_raw or ''}").lower()
    source_brand = _clean_text(brand_raw)

    if "جک" in text or "jac" in text:
        brand = "Jac"
    elif "پژو" in text or "peugeot" in text:
        brand = "Peugeot"
    elif "سمند" in text:
        brand = "Samand"
    elif "دنا" in text:
        brand = "Dena"
    elif "پراید" in text:
        brand = "Pride"
    elif "پارس" in text and "peugeot" not in text and "پژو" not in text:
        brand = "Peugeot"
    else:
        brand = source_brand or None

    model = None
    trim = None

    if brand == "Jac":
        if re.search(r"\bj4\b|جک\s*j4", text):
            model = "J4"
        elif re.search(r"\bs5\b|جک\s*s5", text):
            model = "S5"
        elif re.search(r"\bs3\b|جک\s*s3", text):
            model = "S3"
    elif brand == "Peugeot":
        if "206" in text:
            model = "206"
            if "تیپ 2" in text or "tip 2" in text or "تیپ ۲" in text:
                trim = "Tip 2"
            elif "تیپ 5" in text or "tip 5" in text or "تیپ ۵" in text:
                trim = "Tip 5"
        elif "207" in text:
            model = "207"
        elif "405" in text:
            model = "405"
        elif "پارس" in text or "pars" in text:
            model = "Pars"
        elif "roa" in text or "روآ" in text:
            model = "Roa"
    elif brand == "Samand":
        if "سورن" in text:
            model = "Soren"
        elif "ef7" in text:
            model = "LX EF7"
        elif "lx" in text:
            model = "LX"
    elif brand == "Dena":
        if "پلاس" in text:
            model = "Plus"
        else:
            model = "Dena"
    elif brand == "Pride":
        if "131" in text:
            model = "131"
        elif "132" in text:
            model = "132"
        elif "111" in text:
            model = "111"
        elif "141" in text:
            model = "141"
        elif "وانت" in text or "151" in text:
            model = "151"

    if model is None:
        model = _clean_text(tip_raw) or None
    if trim is None:
        # Keep source detail only when we could not derive an explicit trim.
        trim = None

    return NormalizedCar(brand_name=brand, model_name=model, trim_name=trim)
