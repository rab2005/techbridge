"""AI enrichment: turn dense patent/TTO language into plain-English business summaries."""

import json
from datetime import datetime, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import config

INDUSTRIES = [
    "Therapeutics & Drug Delivery",
    "Diagnostics & Medical Devices",
    "Research Tools & Lab Methods",
    "Digital Health & Health IT",
    "Software & AI",
    "Electronics & Semiconductors",
    "Energy & Clean Tech",
    "Materials & Chemistry",
    "Agriculture & Food",
    "Manufacturing & Robotics",
    "Aerospace & Defense",
    "Environment & Water",
    "Consumer Products",
    "Other",
]

IndustryName = Literal[tuple(INDUSTRIES)]  # type: ignore[valid-type]


class Enrichment(BaseModel):
    """The structured output we ask the model to return for every technology."""

    summary: str = Field(description="2-3 sentence plain-English summary a non-scientist "
                                     "entrepreneur would understand. No jargon.")
    applications: list[str] = Field(description="3-5 things a company could sell, each written as "
                                                "'[what is sold] for [who buys it]', under 15 words.")
    target_customers: list[str] = Field(description="2-4 types of customers who would pay "
                                                    "for products built on this.")
    industry: IndustryName = Field(description="The single best-fitting industry vertical.")
    trl: int = Field(ge=1, le=9, description="Estimated Technology Readiness Level, 1 (basic "
                                             "principle) to 9 (proven in operation).")
    difficulty: Literal["low", "medium", "high"] = Field(
        description="How hard it would be for a startup to commercialize this.")
    difficulty_reason: str = Field(description="One sentence naming the biggest obstacle for a founder. "
                                           "Use 'likely' or 'may' for anything the listing does not state.")


SYSTEM_PROMPT = """You are an analyst who helps entrepreneurs find university technologies to license.
You read technology transfer listings written by scientists and patent attorneys and
explain the business opportunity in plain English.

Guidelines:
- Write for a smart business owner with no science background.
- Be concrete: name real product categories and customer types, not vague benefits.
- Be honest about difficulty. Anything needing FDA approval for a drug or Class III device is "high".
  Software, research reagents, or simple devices that could sell soon are usually "low" or "medium".
- Estimate TRL from the evidence in the listing (lab data only is ~3-4; animal or prototype ~4-5;
  pilot or clinical data ~6-7). If unclear, be conservative.
- Do not invent facts that are not supported by the listing.
- Explain every medical or technical term in everyday words the first time
  you use it, or leave it out. For example, write "eczema" instead of
  "atopic dermatitis".
- Write each application as: [what is being sold] for [who buys it].
  Good: "Eczema drug-testing service for pharmaceutical companies"
  Bad: "Study skin inflammation mechanisms" (a research activity, nothing is sold)
- Only list uses the listing mentions or that follow directly from it. Anything you are inferring must say "likely" or "may".
- Don't claim things about licensing, regulation or markets that the listing doesn't state. If you're making an estimate, say so (“likely”, “may”).
- Mention the biggest obstacle for a founder, including a small or specialized market, not just technical or regulatory hurdles. 
- If only lab results are described, the TRL must be 4 or lower.
- If laws may limit who can buy or use the technology, say so, and do not list customers who may not be allowed to use it.
"""

MAX_ABSTRACT_CHARS = 6000


def build_prompt(tech: dict) -> str:
    categories = tech.get("categories") or []
    if isinstance(categories, str):
        categories = json.loads(categories)
    institution = config.INSTITUTIONS.get(tech["institution"], tech["institution"])
    return (
        f"Institution: {institution}\n"
        f"Title: {tech['title']}\n"
        f"Categories: {', '.join(categories) or 'n/a'}\n\n"
        f"Listing:\n{(tech.get('abstract') or '')[:MAX_ABSTRACT_CHARS]}"
    )


def enrich_one(client: anthropic.Anthropic, tech: dict) -> Enrichment:
    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(tech)}],
        output_format=Enrichment,  # structured output: the reply must match this schema
    )
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to summarize this listing")
    if response.stop_reason == "max_tokens":
        raise ValueError("response was cut off (hit max_tokens)")
    if response.parsed_output is None:
        raise ValueError("Claude returned no parsed output")
    return response.parsed_output


def save_enrichment(conn, tech_id: int, e: Enrichment) -> None:
    conn.execute(
        """UPDATE technologies SET summary=?, applications=?, target_customers=?, industry=?,
               trl=?, difficulty=?, difficulty_reason=?, enriched_at=?,
               embedding=NULL  -- re-embed next time so search uses the new summary
           WHERE id=?""",
        (e.summary, json.dumps(e.applications), json.dumps(e.target_customers), e.industry,
         e.trl, e.difficulty, e.difficulty_reason,
         datetime.now(timezone.utc).isoformat(timespec="seconds"), tech_id),
    )
    conn.commit()


def enrich_pending(conn, client: anthropic.Anthropic | None = None, limit: int | None = None,
                   force: bool = False, log=print) -> int:
    """Enrich technologies that don't have an AI summary yet. Returns how many succeeded."""
    client = client or anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    sql = "SELECT * FROM technologies" + ("" if force else " WHERE enriched_at IS NULL") + " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql)]
    log(f"Enriching {len(rows)} technologies with {config.CLAUDE_MODEL}")

    done = 0
    for i, tech in enumerate(rows, 1):
        try:
            save_enrichment(conn, tech["id"], enrich_one(client, tech))
            done += 1
            log(f"  ({i}/{len(rows)}) {tech['title'][:70]}")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, TypeError):
            # Bad/missing key or model name: every remaining record would fail too.
            # (The SDK raises TypeError when no credentials are configured at all.)
            raise
        except Exception as e:  # keep going; one bad record shouldn't stop the batch
            log(f"  ! ({i}/{len(rows)}) {tech['title'][:50]}: {e}")
    return done
