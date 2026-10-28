"""Data ingestion: pull technology listings from university Tech Transfer Offices.

Each institution exposes an RSS feed listing its technologies. The feed only
has a truncated description, so we also visit each technology's page to get
the full abstract, categories, and licensing contact.
"""

import fcntl
import html
import re
import time
from contextlib import contextmanager
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

from . import config, db


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    return session


def parse_feed(xml_text: str) -> list[dict]:
    """Parse a Technology Publisher RSS feed into a list of basic listings."""
    root = ET.fromstring(xml_text)
    listings = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        pub = item.findtext("pubDate")
        listings.append({
            "title": html.unescape(item.findtext("title") or "").strip(),
            "url": link,
            "case_id": (item.findtext("caseId") or "").strip() or None,
            "rss_description": html.unescape(item.findtext("description") or "").strip(),
            "published_date": parsedate_to_datetime(pub).date().isoformat() if pub else None,
        })
    return listings


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "br"]


def _clean_text(element) -> str:
    """Turn an HTML fragment into readable plain text with paragraph breaks."""
    for tag in element.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    # html.unescape catches entities some sites double-escape (e.g. "&amp;ldquo;").
    text = html.unescape(element.get_text()).replace("\u00a0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    paragraphs = [line for line in lines if line]
    return "\n\n".join(paragraphs)


ROOT_CATEGORY = "technology classifications"


def parse_categories(paths) -> list[str]:
    """['Technology Classifications > Therapeutics > Gene Therapies', 'Optics'] -> ['Gene Therapies', 'Optics']

    Accepts a list of category paths, or one comma-separated string of them.
    """
    if isinstance(paths, str):
        paths = paths.split(",")
    leaves = []
    for path in paths:
        parts = [p.strip() for p in path.split(">") if p.strip()]
        if not parts or (len(parts) == 1 and parts[0].lower() == ROOT_CATEGORY):
            continue
        if parts[-1] not in leaves:
            leaves.append(parts[-1])
    return leaves


# Technology Publisher sites share a backend but universities customise the page
# template, so each field is looked up with a few selectors, most specific first.
DESCRIPTION_SELECTORS = [
    "div.c_tp_description.tech-description",  # JHU
    "div.js-page-description",                # UChicago
    "div.c_tp_description",                   # UPenn, Rochester (pick the longest)
]


def _find_description(soup):
    for selector in DESCRIPTION_SELECTORS:
        matches = soup.select(selector)
        if matches:
            return max(matches, key=lambda m: len(m.get_text()))
    return None


def _find_categories(soup) -> list[str]:
    links = soup.find(id="categoryLinks")                  # JHU: one comma-separated string
    if links and links.get_text(strip=True):
        return parse_categories(links.get_text())
    paths = [a.get_text(" ", strip=True) for a in soup.select("a.catlink")]  # UPenn
    paths += [a.get_text(strip=True) for a in soup.select('a[href*="type=c"]')]  # Rochester
    return parse_categories(paths)


PHONE_RE = re.compile(r"[\d\s().+\-x]{7,}")


def _find_contact(soup) -> dict:
    contact = {"contact_name": None, "contact_email": None, "contact_phone": None}

    def first_text(selector):
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node and node.get_text(strip=True) else None

    def email_in(node):
        mail = node.find("a", href=re.compile(r"^mailto:", re.I)) if node else None
        return mail["href"].split(":", 1)[1].split("?")[0].strip() if mail else None

    def phone_in(node):
        tel = node.find("a", href=re.compile(r"^tel:", re.I)) if node else None
        return tel.find(string=True, recursive=False).strip() if tel else None

    # UPenn: dedicated fields
    if soup.select_one("div.technology-contact"):
        box = soup.select_one("div.technology-contact")
        contact["contact_name"] = first_text(".technology-contact-name")
        contact["contact_email"] = email_in(box)
        contact["contact_phone"] = first_text(".technology-contact-phone")
        return contact

    # UChicago: "Contact <b>Name</b>, Title, ..." plus a list of mailto/tel links
    if soup.select_one(".page-contact-list"):
        name = soup.select_one(".page-contact-text b")
        contact["contact_name"] = name.get_text(strip=True) if name else None
        contact["contact_email"] = email_in(soup.select_one(".page-contact-list"))
        contact["contact_phone"] = phone_in(soup.select_one(".page-contact-list"))
        return contact

    # JHU, Rochester: free text block — name first, then title/org lines, phone, email
    box = soup.select_one("div.c_tp_contact")
    if box:
        contact["contact_email"] = email_in(box)
        # Skip the bold heading ("For Information, Contact:" / "Contact For More Information:").
        label = box.find("b")
        skip = {label.get_text(strip=True) if label else None, contact["contact_email"]}
        lines = [s for s in box.stripped_strings if s not in skip]
        for line in lines:
            if PHONE_RE.fullmatch(line):
                contact["contact_phone"] = contact["contact_phone"] or line
            elif not contact["contact_name"]:
                contact["contact_name"] = line
    return contact


def parse_detail(html: str) -> dict:
    """Extract the full abstract, categories and contact info from a technology page."""
    soup = BeautifulSoup(html, "html.parser")
    abstract = None
    desc = _find_description(soup)
    if desc:
        # The <h1> title sometimes sits inside the description block; drop it.
        for h1 in desc.find_all("h1"):
            h1.decompose()
        abstract = _clean_text(desc) or None
    return {"abstract": abstract, "categories": _find_categories(soup), **_find_contact(soup)}


def scrape_institution(conn, session, slug: str, limit: int | None = None,
                       refresh: bool = False, log=print) -> int:
    """Scrape one institution. Returns the number of technologies saved."""
    resp = session.get(config.feed_url(slug), timeout=30)
    resp.raise_for_status()
    listings = parse_feed(resp.text)
    log(f"[{slug}] feed lists {len(listings)} technologies")

    seen = set() if refresh else db.known_urls(conn)
    todo = [l for l in listings if l["url"] not in seen]
    if limit is not None:
        todo = todo[:limit]

    saved = 0
    for i, listing in enumerate(todo, 1):
        try:
            time.sleep(config.REQUEST_DELAY_SECONDS)
            page = session.get(listing["url"], timeout=30)
            page.raise_for_status()
            detail = parse_detail(page.text)
        except requests.RequestException as e:
            log(f"[{slug}]   ! {listing['url']}: {e}")
            continue

        db.upsert_technology(conn, {
            "institution": slug,
            "case_id": listing["case_id"],
            "title": listing["title"],
            "url": listing["url"],
            "abstract": detail["abstract"] or listing["rss_description"],
            "categories": detail["categories"],
            "contact_name": detail["contact_name"],
            "contact_email": detail["contact_email"],
            "contact_phone": detail["contact_phone"],
            "published_date": listing["published_date"],
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        conn.commit()
        saved += 1
        log(f"[{slug}]   ({i}/{len(todo)}) {listing['title'][:70]}")
    return saved


class ScrapeInProgress(RuntimeError):
    pass


@contextmanager
def scrape_lock():
    """Only one scrape at a time, so overlapping runs never double the load on the TTO sites."""
    lock_path = config.DB_PATH.parent / ".scrape.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScrapeInProgress("another scrape is already running") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def scrape_all(conn, slugs=None, limit=None, refresh=False, log=print) -> int:
    session = make_session()
    total = 0
    with scrape_lock():
        for slug in slugs or config.INSTITUTIONS:
            try:
                total += scrape_institution(conn, session, slug, limit, refresh, log)
            except (requests.RequestException, ET.ParseError) as e:
                log(f"[{slug}] failed to read feed: {e}")
    return total
