from collections.abc import Generator

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://seenad_db:seenad_db@localhost:5432/seenad_db"
    postgres_db: str = "seenad_db"
    postgres_user: str = "seenad_db"
    postgres_password: str = "seenad_db"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()


class Base(DeclarativeBase):
    pass


def _build_engine_url() -> str | URL:
    # In containerized deployments, passwords may include special characters
    # (e.g. "@") and break plain string DATABASE_URL parsing.
    # Building URL from components keeps credentials safe and correctly encoded.
    if settings.postgres_host != "localhost" or settings.postgres_port != 5432:
        return URL.create(
            drivername="postgresql+psycopg2",
            username=settings.postgres_user,
            password=settings.postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        )
    if settings.database_url:
        return settings.database_url
    return URL.create(
        drivername="postgresql+psycopg2",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


engine = create_engine(_build_engine_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
