"""FastAPI backend: search, filters, detail pages, and reverse ("describe your problem") search."""

import re

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db
from .embeddings import VectorIndex, default_embedder

app = FastAPI(title="TechBridge")
conn = db.connect()

# Columns returned in result lists (detail pages get everything).
LIST_COLUMNS = """t.id, t.institution, t.title, t.url, t.categories, t.summary, t.applications, 
                t.industry, t.trl, t.difficulty, t.licensing_status, t.published_date,
                  substr(t.abstract, 1, 320) AS abstract_snippet"""


def fts_query(text: str, mode: str = "AND") -> str | None:
    """Turn free text into a safe FTS5 query: every word becomes a quoted prefix term."""
    words = re.findall(r"\w+", text.lower())
    if not words:
        return None
    return f" {mode} ".join(f'"{w}"*' for w in words)


def with_institution_name(d: dict) -> dict:
    d["institution_name"] = config.INSTITUTIONS.get(d["institution"], d["institution"])
    return d


@app.get("/api/technologies")
def search_technologies(
    q: str = "",
    institution: str | None = None,
    industry: str | None = None,
    difficulty: str | None = None,
    status: str | None = None,
    trl_min: int | None = Query(None, ge=1, le=9),
    trl_max: int | None = Query(None, ge=1, le=9),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    where, params = [], []
    match = fts_query(q)
    if match:
        where.append("technologies_fts MATCH ?")
        params.append(match)
    for column, value in (("institution", institution), ("industry", industry), ("difficulty", difficulty), ("licensing_status", status)):
        if value:
            where.append(f"t.{column} = ?")
            params.append(value)
    if trl_min is not None:
        where.append("t.trl >= ?")
        params.append(trl_min)
    if trl_max is not None:
        where.append("t.trl <= ?")
        params.append(trl_max)

    joins = "JOIN technologies_fts ON technologies_fts.rowid = t.id" if match else ""
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order = "bm25(technologies_fts)" if match else "t.published_date DESC, t.id DESC"

    total = conn.execute(f"SELECT COUNT(*) FROM technologies t {joins} {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {LIST_COLUMNS} FROM technologies t {joins} {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [with_institution_name(db.row_to_dict(r)) for r in rows],
    }


@app.get("/api/technologies/{tech_id}")
def get_technology(tech_id: int):
    row = conn.execute("SELECT * FROM technologies WHERE id = ?", (tech_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Technology not found")
    return with_institution_name(db.row_to_dict(row))


@app.get("/api/facets")
def facets():
    def counts(column):
        return [
            {"value": r[0], "count": r[1]}
            for r in conn.execute(
                f"SELECT {column}, COUNT(*) FROM technologies WHERE {column} IS NOT NULL "
                f"GROUP BY {column} ORDER BY COUNT(*) DESC")
        ]

    institutions = [
        {**c, "name": config.INSTITUTIONS.get(c["value"], c["value"])} for c in counts("institution")
    ]
    stats = conn.execute(
        "SELECT COUNT(*), COUNT(enriched_at), COUNT(embedding) FROM technologies").fetchone()
    return {
        "institutions": institutions,
        "industries": counts("industry"),
        "difficulties": counts("difficulty"),
        "stats": {"total": stats[0], "enriched": stats[1], "embedded": stats[2]},
        "semantic_search": stats[2] > 0,
    }


# ---- Reverse search -------------------------------------------------------

class MatchRequest(BaseModel):
    problem: str = Field(min_length=3, max_length=2000)
    limit: int = Field(10, ge=1, le=50)


_index_cache: dict = {"count": -1, "index": None}


def vector_index() -> VectorIndex:
    """Reload the embedding matrix only when the number of embedded rows changes."""
    count = conn.execute("SELECT COUNT(embedding) FROM technologies").fetchone()[0]
    if count != _index_cache["count"]:
        _index_cache.update(count=count, index=VectorIndex(conn))
    return _index_cache["index"]


@app.post("/api/match")
def match_problem(req: MatchRequest):
    index = vector_index()
    if len(index):
        query_vec = default_embedder().query(req.problem)
        hits = index.search(query_vec, req.limit)
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT {LIST_COLUMNS} FROM technologies t WHERE t.id IN ({','.join('?' * len(hits))})",
            [h[0] for h in hits])}
        results = [{**with_institution_name(db.row_to_dict(rows[i])), "score": round(s, 3)}
                   for i, s in hits if i in rows]
        return {"mode": "semantic", "results": results}

    # Fallback before `python -m techbridge embed` has run: keyword search matching ANY of the words.
    match = fts_query(req.problem, mode="OR")
    if not match:
        return {"mode": "keyword", "results": []}
    rows = conn.execute(
        f"""SELECT {LIST_COLUMNS} FROM technologies t
            JOIN technologies_fts ON technologies_fts.rowid = t.id
            WHERE technologies_fts MATCH ? ORDER BY bm25(technologies_fts) LIMIT ?""",
        (match, req.limit)).fetchall()
    return {"mode": "keyword", "results": [with_institution_name(db.row_to_dict(r)) for r in rows]}


app.mount("/", StaticFiles(directory=config.ROOT / "static", html=True), name="static")
