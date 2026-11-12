from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from techbridge import config, db, embeddings, enrich, scraper

FIXTURES = Path(__file__).parent / "fixtures"


# ---- Scraper ---------------------------------------------------------------

def test_parse_feed():
    items = scraper.parse_feed((FIXTURES / "jhu_feed.xml").read_text())
    assert len(items) == 3
    first = items[0]
    assert first["title"].startswith("A Transgenic Mouse Model")
    assert first["url"] == "https://jhu.technologypublisher.com/technology/63274"
    assert first["case_id"] == "C11268"
    assert first["published_date"] == "2026-10-01"


def test_parse_detail():
    d = scraper.parse_detail((FIXTURES / "jhu_detail.html").read_text())
    assert d["abstract"].startswith("Value Proposition")
    assert "lipid nanoparticle" in d["abstract"]
    assert len(d["abstract"]) > 1000  # full text, not the truncated RSS version
    assert d["categories"] == ["Gene Therapies", "Small Molecules", "Therapeutic Delivery Platforms"]
    assert d["contact_name"] == "Vera Prokic"
    assert d["contact_email"] == "vsampel2@jhu.edu"
    assert d["contact_phone"] == "410-614-0300"


@pytest.mark.parametrize("slug, min_len, categories, name, email, phone", [
    ("uchicago", 2500, [], "Michael Hinton", "mhinton@uchicago.edu", "773-834-9151"),
    ("upenn", 3000, ["Research Tools & Reagents", "Therapeutics"], "Gangotri Dey", "gdey6@upenn.edu", None),
    ("rochester", 2500, ["Devices", "Optics", "Research Tools"], "Curtis Broadbent",
     "curtis.broadbent@rochester.edu", "585.273.3250"),
    ("arizona", 1500, [], "Scott Zentack", "zentack@arizona.edu", None),
])
def test_parse_detail_other_templates(slug, min_len, categories, name, email, phone):
    """Each university customises its page template; all should parse."""
    d = scraper.parse_detail((FIXTURES / f"{slug}_detail.html").read_text())
    assert len(d["abstract"]) > min_len
    assert d["categories"] == categories
    assert (d["contact_name"], d["contact_email"], d["contact_phone"]) == (name, email, phone)


def test_parse_categories_skips_root_and_duplicates():
    raw = "Technology Classifications, A > B > Sensors, A > C > Sensors, X > Y, "
    assert scraper.parse_categories(raw) == ["Sensors", "Y"]
    assert scraper.parse_categories(["Optics", "Technology Classifications"]) == ["Optics"]


# ---- Database + AI pipeline (Claude and the embedding model are faked) -------

def sample_tech(i, title, abstract, institution="jhu"):
    return {
        "institution": institution, "case_id": f"C{i}", "title": title,
        "url": f"https://example.edu/tech/{i}", "abstract": abstract,
        "categories": ["Sensors"], "contact_name": "Pat Lee", "contact_email": "pat@example.edu",
        "contact_phone": None, "published_date": f"2026-09-0{i}", "scraped_at": "2026-10-04T00:00:00",
    }


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.upsert_technology(c, sample_tech(1, "Soil moisture sensor", "A low-cost sensor for irrigation."))
    db.upsert_technology(c, sample_tech(2, "Battery electrolyte", "A safer lithium battery electrolyte.", "upenn"))
    c.commit()
    return c


class FakeClaude:
    """Stands in for anthropic.Anthropic: returns a canned structured-output response."""

    def __init__(self, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.messages = SimpleNamespace(parse=self._parse)
        self.calls = []

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["output_format"] is enrich.Enrichment
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=enrich.Enrichment(
            summary="A cheap sensor that tells farmers when to water.",
            applications=["Smart irrigation controllers", "Greenhouse monitoring", "Golf course care"],
            target_customers=["Farms", "Irrigation equipment makers"],
            industry="Agriculture & Food", trl=4, difficulty="low",
            difficulty_reason="Simple hardware with no regulatory approval needed."))


class FakeEmbedder:
    """Stands in for the local embedding model with deterministic 2-D vectors."""

    model_name = "fake"

    @staticmethod
    def _vec(text):
        water = any(w in text.lower() for w in ("soil", "water", "crop"))
        return np.array([1.0, 0.1] if water else [0.1, 1.0], dtype=np.float32)

    def documents(self, texts):
        return embeddings._normalise(np.vstack([self._vec(t) for t in texts]))

    def query(self, text):
        return embeddings._normalise(self._vec(text)[None, :])[0]


def test_upsert_is_idempotent(conn):
    db.upsert_technology(conn, sample_tech(1, "Soil moisture sensor v2", "Updated."))
    rows = conn.execute("SELECT title FROM technologies WHERE url LIKE '%/1'").fetchall()
    assert [r["title"] for r in rows] == ["Soil moisture sensor v2"]


def test_enrich_and_embed(conn):
    fake = FakeClaude()
    assert enrich.enrich_pending(conn, client=fake, log=lambda *_: None) == 2
    assert fake.calls[0]["model"] == config.CLAUDE_MODEL
    assert "Soil moisture sensor" in fake.calls[0]["messages"][0]["content"]
    row = db.row_to_dict(conn.execute("SELECT * FROM technologies WHERE id = 1").fetchone())
    assert row["industry"] == "Agriculture & Food"
    assert row["applications"][0] == "Smart irrigation controllers"
    assert row["trl"] == 4
    # Already-enriched rows are skipped on the next run.
    assert enrich.enrich_pending(conn, client=fake, log=lambda *_: None) == 0

    embedder = FakeEmbedder()
    assert embeddings.embed_pending(conn, embedder=embedder, log=lambda *_: None) == 2
    index = embeddings.VectorIndex(conn)
    query = embedder.query("my crops dry out")
    best_id, score = index.search(query, k=1)[0]
    assert best_id == 1 and score > 0.9


def test_refusals_are_skipped_not_saved(conn):
    assert enrich.enrich_pending(conn, client=FakeClaude("refusal"), log=lambda *_: None) == 0
    assert conn.execute("SELECT COUNT(enriched_at) FROM technologies").fetchone()[0] == 0


def test_enrichment_schema_is_api_compatible():
    """The SDK must be able to turn our Pydantic model into a structured-output schema."""
    from anthropic.lib._parse._transform import transform_schema
    schema = transform_schema(enrich.Enrichment.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(enrich.Enrichment.model_fields)
    assert "Agriculture & Food" in schema["properties"]["industry"]["enum"]


def test_fts_updates_after_enrichment(conn):
    enrich.enrich_pending(conn, client=FakeClaude(), log=lambda *_: None)
    hits = conn.execute("SELECT rowid FROM technologies_fts WHERE technologies_fts MATCH 'farmers'").fetchall()
    assert hits  # the AI summary text is searchable


# ---- API -------------------------------------------------------------------

@pytest.fixture
def client(conn, monkeypatch):
    from techbridge import api
    monkeypatch.setattr(api, "conn", conn)
    monkeypatch.setattr(api, "_index_cache", {"count": -1, "index": None})
    return TestClient(api.app)


def test_search_keyword_and_filters(client):
    r = client.get("/api/technologies", params={"q": "batter"}).json()  # prefix match
    assert [t["title"] for t in r["results"]] == ["Battery electrolyte"]
    assert r["results"][0]["institution_name"] == "University of Pennsylvania"

    r = client.get("/api/technologies", params={"institution": "jhu"}).json()
    assert r["total"] == 1

    # Characters that are FTS5 syntax must not cause errors.
    assert client.get("/api/technologies", params={"q": 'sensor" OR (*'}).status_code == 200


def test_detail_and_404(client):
    t = client.get("/api/technologies/1").json()
    assert t["contact_email"] == "pat@example.edu"
    assert "embedding" not in t
    assert client.get("/api/technologies/999").status_code == 404


def test_match_falls_back_to_keywords_before_embedding(client):
    r = client.post("/api/match", json={"problem": "I need a better battery"}).json()
    assert r["mode"] == "keyword"
    assert r["results"][0]["title"] == "Battery electrolyte"


def test_match_semantic(client, conn, monkeypatch):
    from techbridge import api
    fake = FakeEmbedder()
    embeddings.embed_pending(conn, embedder=fake, log=lambda *_: None)
    monkeypatch.setattr(api, "default_embedder", lambda: fake)
    r = client.post("/api/match", json={"problem": "Farmers can't tell when soil is dry"}).json()
    assert r["mode"] == "semantic"
    assert r["results"][0]["title"] == "Soil moisture sensor"
    assert r["results"][0]["score"] > 0.9


def test_random_redirects_to_a_technology(client):
    r = client.get("/random", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] in {"/tech.html?id=1", "/tech.html?id=2"}


def test_pages_are_revalidated(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_facets(client):
    f = client.get("/api/facets").json()
    assert f["stats"]["total"] == 2
    assert {i["value"] for i in f["institutions"]} == {"jhu", "upenn"}


def test_scrape_lock_blocks_overlapping_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    with scraper.scrape_lock():
        with pytest.raises(scraper.ScrapeInProgress):
            with scraper.scrape_lock():
                pass
    with scraper.scrape_lock():  # released after the first run finishes
        pass


def test_enrichment_clears_stale_embedding(conn):
    embeddings.embed_pending(conn, embedder=FakeEmbedder(), log=lambda *_: None)
    enrich.enrich_pending(conn, client=FakeClaude(), log=lambda *_: None)
    assert conn.execute("SELECT COUNT(embedding) FROM technologies").fetchone()[0] == 0

def test_update_listing_status(conn):
    def status(url):
        return conn.execute("SELECT licensing_status FROM technologies WHERE url = ?",
                            (url,)).fetchone()[0]

    jhu_url = "https://example.edu/tech/1"
    upenn_url = "https://example.edu/tech/2"

    db.update_listing_status(conn, "jhu", set())
    assert status(jhu_url) == "no longer listed"
    assert status(upenn_url) == "available"  # other universities aren't touched

    db.update_listing_status(conn, "jhu", {jhu_url})
    assert status(jhu_url) == "available"  # it came back

def test_empty_feed_does_not_mark_everything_gone(conn):
    class FakeSession:
        def get(self, url, timeout):
            return SimpleNamespace(text="<rss><channel></channel></rss>",
                                   raise_for_status=lambda: None)

    scraper.scrape_institution(conn, FakeSession(), "jhu", log=lambda *_: None)
    row = conn.execute("SELECT licensing_status FROM technologies WHERE institution = 'jhu'").fetchone()
    assert row[0] == "available"

def test_search_filter_by_status(client, conn):
    db.update_listing_status(conn, "jhu", set())
    r = client.get("/api/technologies", params={"status": "no longer listed"}).json()
    assert [t["title"] for t in r["results"]] == ["Soil moisture sensor"]
    assert r["results"][0]["licensing_status"] == "no longer listed"