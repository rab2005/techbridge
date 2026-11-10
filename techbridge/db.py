"""SQLite storage. One `technologies` table plus an FTS5 index for keyword search."""

import json
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS technologies (
    id               INTEGER PRIMARY KEY,
    institution      TEXT NOT NULL,          -- slug, e.g. "jhu"
    case_id          TEXT,
    title            TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    abstract         TEXT,                   -- original description, plain text
    categories       TEXT DEFAULT '[]',      -- JSON list from the TTO site
    contact_name     TEXT,
    contact_email    TEXT,
    contact_phone    TEXT,
    published_date   TEXT,                   -- ISO date the TTO published it
    licensing_status TEXT DEFAULT 'available',
    scraped_at       TEXT NOT NULL,

    -- AI enrichment (filled in by enrich.py)
    summary            TEXT,
    applications       TEXT,                 -- JSON list
    target_customers   TEXT,                 -- JSON list
    industry           TEXT,
    trl                INTEGER,              -- technology readiness level 1-9
    difficulty         TEXT,                 -- low / medium / high
    difficulty_reason  TEXT,
    enriched_at        TEXT,

    -- Semantic search (filled in by embeddings.py)
    embedding          BLOB                  -- float32 vector
);

CREATE INDEX IF NOT EXISTS idx_tech_institution ON technologies(institution);
CREATE INDEX IF NOT EXISTS idx_tech_industry ON technologies(industry);

CREATE VIRTUAL TABLE IF NOT EXISTS technologies_fts USING fts5(
    title, abstract, summary, categories,
    content='technologies', content_rowid='id', tokenize='porter'
);

-- Keep the full-text index in sync with the main table.
CREATE TRIGGER IF NOT EXISTS tech_ai AFTER INSERT ON technologies BEGIN
    INSERT INTO technologies_fts(rowid, title, abstract, summary, categories)
    VALUES (new.id, new.title, new.abstract, new.summary, new.categories);
END;
CREATE TRIGGER IF NOT EXISTS tech_ad AFTER DELETE ON technologies BEGIN
    INSERT INTO technologies_fts(technologies_fts, rowid, title, abstract, summary, categories)
    VALUES ('delete', old.id, old.title, old.abstract, old.summary, old.categories);
END;
CREATE TRIGGER IF NOT EXISTS tech_au AFTER UPDATE OF title, abstract, summary, categories ON technologies BEGIN
    INSERT INTO technologies_fts(technologies_fts, rowid, title, abstract, summary, categories)
    VALUES ('delete', old.id, old.title, old.abstract, old.summary, old.categories);
    INSERT INTO technologies_fts(rowid, title, abstract, summary, categories)
    VALUES (new.id, new.title, new.abstract, new.summary, new.categories);
END;
"""

JSON_FIELDS = ("categories", "applications", "target_customers")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait up to 30s if another process (e.g. the scheduled update) is writing.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        # WAL lets the web app keep reading while a scrape is writing.
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a DB row to a JSON-friendly dict (decodes JSON columns, drops the embedding)."""
    d = dict(row)
    d.pop("embedding", None)
    for field in JSON_FIELDS:
        if field in d:
            d[field] = json.loads(d[field]) if d[field] else []
    return d


def upsert_technology(conn: sqlite3.Connection, tech: dict) -> None:
    """Insert a scraped technology, or refresh its scraped fields if the URL already exists.

    AI-enriched fields are left untouched on update.
    """
    conn.execute(
        """
        INSERT INTO technologies (institution, case_id, title, url, abstract, categories,
                                  contact_name, contact_email, contact_phone,
                                  published_date, scraped_at)
        VALUES (:institution, :case_id, :title, :url, :abstract, :categories,
                :contact_name, :contact_email, :contact_phone,
                :published_date, :scraped_at)
        ON CONFLICT(url) DO UPDATE SET
            case_id = excluded.case_id,
            title = excluded.title,
            abstract = excluded.abstract,
            categories = excluded.categories,
            contact_name = excluded.contact_name,
            contact_email = excluded.contact_email,
            contact_phone = excluded.contact_phone,
            published_date = excluded.published_date,
            scraped_at = excluded.scraped_at
        """,
        {**tech, "categories": json.dumps(tech.get("categories", []))},
    )


def known_urls(conn: sqlite3.Connection) -> set[str]:
    return {r["url"] for r in conn.execute("SELECT url FROM technologies")}

def update_listing_status(conn: sqlite3.Connection, institution: str, feed_urls: set[str]) -> None:
    """Mark each saved technology by whether it is still in the institution's feed."""
    rows = conn.execute("SELECT url FROM technologies WHERE institution = ?",
                        (institution,)).fetchall()
    for row in rows:
        status = "available" if row["url"] in feed_urls else "no longer listed"
        conn.execute("UPDATE technologies SET licensing_status = ? WHERE url = ?",
                     (status, row["url"]))
    conn.commit()