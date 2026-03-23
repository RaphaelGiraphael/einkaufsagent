"""
Einstiegspunkt für den Böck Einkaufsagenten.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from db.schema import init_db
from bot.telegram_bot import build_application

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN fehlt in der .env Datei")

    db_path = os.getenv("DB_PATH", "boeck_agent.db")
    init_db(db_path)
    logger.info("Datenbank bereit: %s", db_path)

    app = build_application(token)
    logger.info("Bot startet (Polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Python 3.12+ legt keinen Event-Loop mehr automatisch an
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
