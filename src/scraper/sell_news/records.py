"""Dataclasses for one article and its vehicle rows."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class CarRow:
    row_index: int
    brand_raw: str | None = None
    model_raw: str | None = None
    trim_raw: str | None = None
    colors_raw: str | None = None
    price_raw: str | None = None
    price_toman: int | None = None
    price_unit_guess: str | None = None
    plan_name_raw: str | None = None
    delivery_raw: str | None = None
    extra_json: dict[str, Any] = field(default_factory=dict)
    extract_method: str = "html_table"

    def is_empty(self) -> bool:
        return not any(
            (
                self.brand_raw,
                self.model_raw,
                self.trim_raw,
                self.colors_raw,
                self.price_raw,
                self.delivery_raw,
                self.plan_name_raw,
            )
        )

    def mark_incomplete(self) -> None:
        missing_vehicle = not (self.brand_raw or self.model_raw)
        missing_deal = not (self.price_raw or self.delivery_raw)
        if missing_vehicle or missing_deal:
            self.extra_json = {**self.extra_json, "incomplete": True}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewsArticle:
    source: str
    canonical_url: str
    title: str
    source_article_id: str | None = None
    source_list_url: str | None = None
    category: str = "بخشنامه"
    subtitle: str | None = None
    full_text: str | None = None
    full_html: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    image_url: str | None = None
    company_name_raw: str | None = None
    sale_type: str | None = None
    sale_type_raw: str | None = None
    campaign_badge: str | None = None
    matched_keywords: list[str] | None = None
    raw_json: dict[str, Any] | None = None
    parse_ok: bool = False
    parse_error: str | None = None
    http_status: int | None = None
    cars: list[CarRow] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] | None = None
    content_blocks_version: int = 1

    def finalize_parse_ok(self) -> None:
        has_text = False
        for block in self.content_blocks or []:
            kind = block.get("type")
            if kind == "list" and any(str(x).strip() for x in (block.get("items") or [])):
                has_text = True
                break
            if kind in {"heading", "paragraph", "quote", "note"} and str(block.get("text") or "").strip():
                has_text = True
                break
        self.parse_ok = bool(self.title and self.canonical_url and (has_text or self.cars))

    def content_fingerprint(self) -> tuple[str, str, str]:
        return (
            self.title or "",
            (self.full_text or "")[:4000],
            self.published_at.isoformat() if self.published_at else "",
        )

    def as_sample_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.published_at:
            payload["published_at"] = self.published_at.isoformat()
        if self.modified_at:
            payload["modified_at"] = self.modified_at.isoformat()
        if self.full_html and len(self.full_html) > 2500:
            payload["full_html"] = self.full_html[:2500] + "…"
        if self.full_text and len(self.full_text) > 4000:
            payload["full_text"] = self.full_text[:4000] + "…"
        return payload
