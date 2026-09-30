"""Scraper parsing tests using HTML captured from the live site."""
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bellhaven_sync.scraper import community_links, parse_detail  # noqa: E402

DETAIL = """<div class="wrap"><h1>Bellhaven Meadows of Findlay</h1><dl class="detail">
<dt>Address</dt><dd>1800 N Blanchard St<br>Findlay, OH 45840</dd>
<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>
<dt>Administrator</dt><dd>Sam Pruitt</dd><dt>Phone</dt><dd>(231) 533-2969</dd></dl></div>"""

LISTING = """<p>Page 3 of 3 · 34 communities listed</p><div class="card"><h3>
<a href="/communities/amberly-manor">Amberly Manor</a></h3></div>
<div class="pager"><a href="/communities?page=2">&larr; Previous</a><span>Page 3 / 3</span></div>"""


def test_parse_detail():
    loc = parse_detail(BeautifulSoup(DETAIL, "html.parser"), "bellhaven-meadows-of-findlay")
    assert loc == {"slug": "bellhaven-meadows-of-findlay", "name": "Bellhaven Meadows of Findlay",
                   "street": "1800 N Blanchard St", "city": "Findlay", "state": "OH", "zip": "45840",
                   "offerings": ["Assisted Living", "Memory Support"], "administrator": "Sam Pruitt",
                   "phone": "(231) 533-2969"}


def test_listing_links_ignore_pager():
    assert community_links(BeautifulSoup(LISTING, "html.parser")) == {"/communities/amberly-manor"}
