"""Scrape every Bellhaven community from the public website."""
from __future__ import annotations

import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE_URL = os.environ.get("SITE_URL", "https://analyst-assessment-production.up.railway.app")
_PAGER = re.compile(r"Page\s+(\d+)\s*/\s*(\d+)")
_CITY_LINE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")


class ScrapeError(Exception):
    pass


def _get(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def community_links(soup):
    return {a["href"].split("?")[0].rstrip("/") for a in soup.select('a[href^="/communities/"]')}


def parse_detail(soup, slug):
    h1 = soup.find("h1")
    fields = {}
    for dt in soup.select("dl.detail dt"):
        fields[dt.get_text(strip=True).lower()] = dt.find_next_sibling("dd")
    addr = fields.get("address")
    if not h1 or addr is None:
        raise ScrapeError(f"{slug}: detail page missing name or address")
    lines = [s.strip() for s in addr.get_text("\n").split("\n") if s.strip()]
    m = _CITY_LINE.match(lines[-1]) if lines else None
    if not m or len(lines) < 2:
        raise ScrapeError(f"{slug}: unparseable address {lines!r}")
    care = fields.get("care offerings")
    return {
        "slug": slug,
        "name": h1.get_text(strip=True),
        "street": " ".join(lines[:-1]),
        "city": m["city"].strip(),
        "state": m["state"],
        "zip": m["zip"],
        "offerings": [b.get_text(strip=True) for b in care.select(".badge")] if care else [],
        "administrator": fields["administrator"].get_text(strip=True) if fields.get("administrator") else "",
        "phone": fields["phone"].get_text(strip=True) if fields.get("phone") else "",
    }


def scrape(site_url: str = SITE_URL, session=None):
    """Returns (locations, meta). Walks the paginated directory using the site's own
    'Page X / N' marker (the site clamps out-of-range pages instead of returning empty ones,
    so 'loop until empty' would never stop), and ALSO collects community links from the home
    page, because new communities can be featured there before they reach the directory."""
    session = session or requests.Session()
    base = site_url.rstrip("/")
    links, pages, listed_count = set(), 1, None
    page = 1
    while page <= pages:
        soup = _get(session, f"{base}/communities?page={page}")
        m = _PAGER.search(soup.get_text(" "))
        if m:
            pages = int(m.group(2))
        cm = re.search(r"(\d+)\s+communities listed", soup.get_text(" "))
        if cm:
            listed_count = int(cm.group(1))
        links |= community_links(soup)
        page += 1
    directory_links = set(links)
    home = _get(session, base + "/")
    links |= community_links(home)

    locations = []
    for href in sorted(links):
        slug = href.rsplit("/", 1)[-1]
        loc = parse_detail(_get(session, urljoin(base + "/", href.lstrip("/"))), slug)
        loc["source"] = "directory" if href in directory_links else "homepage only"
        locations.append(loc)
    meta = {"directory_pages": pages, "directory_count_claimed": listed_count,
            "directory_links": len(directory_links), "total": len(locations)}
    if listed_count is not None and len(directory_links) != listed_count:
        raise ScrapeError(f"Directory claims {listed_count} communities but {len(directory_links)} were found")
    return locations, meta
