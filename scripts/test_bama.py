import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.scrapers.bama_scraper import scrape_bama_brand


LOGGER = logging.getLogger("bama-test")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    _configure_logging()

    max_attempts = 3
    base_delay_seconds = 12

    db = SessionLocal()
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                LOGGER.info("Attempt %s/%s - starting real crawl for bama.ir", attempt, max_attempts)
                LOGGER.info("Step 1: open target page and load first listings.")
                LOGGER.info("Step 2: parse cards and extract price/year/mileage/body condition.")
                LOGGER.info("Step 3: save records in scraped_listings table.")

                result = scrape_bama_brand(
                    db,
                    brand_slug="jac",
                    model_slug="j4",
                    min_listings=20,
                    headless=True,
                )

                LOGGER.info("Crawl completed successfully.")
                print(f"Cards found: {result.cards_found}")
                print(f"Processed listings: {result.processed_count}")
                print(f"Sample extracted data: {result.sample_listing}")
                break
            except Exception:
                LOGGER.exception("Crawl failed on attempt %s.", attempt)
                if attempt >= max_attempts:
                    raise
                sleep_seconds = base_delay_seconds * attempt
                LOGGER.warning("Retrying after %s seconds due to network/timeout failure.", sleep_seconds)
                time.sleep(sleep_seconds)
    finally:
        db.close()


if __name__ == "__main__":
    main()
