"""Central configuration: paths, API settings, and the list of institutions to scrape."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("TECHBRIDGE_DB", ROOT / "data" / "techbridge.db"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH

# The Anthropic SDK reads ANTHROPIC_API_KEY from the environment (.env is loaded above).
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

# Embeddings run locally (free, offline after a one-time ~100MB model download).
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

# Identify ourselves honestly to the sites we crawl.
USER_AGENT = "TechBridge-student-project/0.1 (educational; tech transfer discovery)"
REQUEST_DELAY_SECONDS = 1.0  # be polite: at most ~1 request/second per run

# Many universities host their available-technology listings on Inteum's
# "Technology Publisher" platform. They all share one RSS + page format,
# so a single scraper covers every institution below.
INSTITUTIONS = {
    "jhu": "Johns Hopkins University",
    "upenn": "University of Pennsylvania",
    "uchicago": "University of Chicago",
    "rochester": "University of Rochester",
    "arizona": "University of Arizona",
}


def feed_url(slug: str) -> str:
    return f"https://{slug}.technologypublisher.com/rss.aspx"
