from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UsdIrrRate(Base):
    """Daily free-market USD/IRR snapshot (017_usd_irr_rate.sql)."""

    __tablename__ = "usd_irr_rate"
    __table_args__ = (UniqueConstraint("as_of_date", "source", name="uq_usd_irr_rate_as_of_date_source"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    rate_irr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rate_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="tgju", index=True)
    instrument_key: Mapped[str] = mapped_column(String(80), nullable=False, default="price_dollar_rl")
    quoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    high_irr: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    low_irr: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_irr: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


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


class IranJibZeroPriceScrapeSnapshot(Base):
    """One raw IranJib catalogue row per batch (021_iranjib_zero_price_scrape_snapshot.sql)."""

    __tablename__ = "iranjib_zero_price_scrape_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, index=True)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    page_kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    section_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    iranjib_iid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_price_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    factory_price_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    change_amount_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    change_percent_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    market_price_toman: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    factory_price_toman: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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


class CarSellNews(Base):
    """One sell-condition / بخشنامه article (018_car_sell_news.sql)."""

    __tablename__ = "car_sell_news"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_article_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_list_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="بخشنامه")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_name_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    sale_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    sale_type_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_badge: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    parse_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    scrape_batch_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    content_blocks: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    content_blocks_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    cars: Mapped[list["CarSellNewsCar"]] = relationship(
        back_populates="news",
        cascade="all, delete-orphan",
    )


class CarSellNewsCar(Base):
    """Vehicle row extracted from a sell-news article."""

    __tablename__ = "car_sell_news_cars"
    __table_args__ = (UniqueConstraint("news_id", "row_index", name="uq_car_sell_news_cars_news_id_row_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("car_sell_news.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    brand_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    trim_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    colors_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_toman: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_unit_guess: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_name_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extract_method: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    news: Mapped[CarSellNews] = relationship(back_populates="cars")
