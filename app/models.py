from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CarEntity(Base):
    """Reference row for calculator / listings (Bama taxonomy: brand → model → trim)."""

    __tablename__ = "car_entities"
    __table_args__ = (
        UniqueConstraint(
            "brand",
            "model",
            "trim",
            name="uq_car_entities_brand_model_trim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    brand: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    trim: Mapped[str] = mapped_column(String(160), nullable=False)
    production_year_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    production_year_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(String(300), nullable=False, default="https://bama.ir/calculator")


class Color(Base):
    __tablename__ = "colors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


class GearboxType(Base):
    __tablename__ = "gearbox_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


class ListingClean(Base):
    """Denormalized row synced from scraped_listings; table created in postgres/migrations/005_listings_clean.sql."""

    __tablename__ = "listings_clean"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, index=True)
    scraped_listing_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("scraped_listings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    brand_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    mileage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    color_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gearbox_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    car_reference_id: Mapped[int | None] = mapped_column(
        ForeignKey("car_entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ScrapedListing(Base):
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
    car_reference_id: Mapped[int | None] = mapped_column(
        ForeignKey("car_entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    color_id: Mapped[int | None] = mapped_column(
        ForeignKey("colors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gearbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("gearbox_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # DB column is scraped_at (not created_at); omit on INSERT — PostgreSQL fills via server_default.
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
