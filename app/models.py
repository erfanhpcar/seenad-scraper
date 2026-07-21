from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CarZeroPriceScrapeSnapshot(Base):
    """One raw hierarchy row from Bama CAD API per batch (012_car_zero_price_scrape_snapshot.sql)."""

    __tablename__ = "car_zero_price_scrape_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, index=True)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    hierarchy_page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    brand_name_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brand_slug_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_name_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_slug_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trim_name_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trim_slug_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_year_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    price_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    price_provider: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bama_car_price_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class ScrapedListing(Base):
    """Raw listing staging row (Bama, Karnameh, Hamrah Mechanic, Divar MSSQL ingest)."""

    __tablename__ = "scraped_listings"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_scraped_listings_source_external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(150), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True, index=True)
    mileage: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    brand_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    model_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    trim_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
