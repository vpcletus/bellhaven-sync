"""Daily pipeline: scrape -> snapshot CRM -> match -> queue proposals. READ-ONLY against the CRM.

Usage:  python -m bellhaven_sync.pipeline
Env:    CRM_TOKEN (required), CRM_BASE_URL, SITE_URL, SYNC_DB
"""
from __future__ import annotations

import json
import os
import sys
import time

from .crm import CRMClient, find_parent_id
from .matcher import Matcher
from .scraper import SITE_URL, scrape
from .store import Store

SNAP_DIR = os.environ.get("SNAPSHOT_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "snapshots"))
MIN_SCRAPE_RATIO = 0.8  # abort if the site suddenly yields far fewer communities than last run


def run(store: Store | None = None, client: CRMClient | None = None, locations=None, verbose=True):
    store = store or Store()
    client = client or CRMClient()
    run_id = store.start_run()
    try:
        prev = store.db.execute("SELECT n_locations FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        meta = {}
        if locations is None:
            locations, meta = scrape(SITE_URL)
        if prev and prev["n_locations"] and len(locations) < MIN_SCRAPE_RATIO * prev["n_locations"]:
            raise RuntimeError(f"Scrape returned {len(locations)} communities vs {prev['n_locations']} last run; "
                               f"refusing to propose 'not on website' changes from a possibly broken scrape.")
        accounts = client.list_accounts()
        contacts = client.list_contacts()
        parent_id = find_parent_id(accounts)

        os.makedirs(SNAP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(SNAP_DIR, f"run{run_id}-{stamp}.json"), "w") as f:
            json.dump({"meta": meta, "locations": locations, "accounts": accounts, "contacts": contacts}, f, indent=1)

        proposals, report = Matcher(locations, accounts, contacts, parent_id, SITE_URL).run()
        stats = store.upsert_proposals(run_id, proposals)
        store.finish_run(run_id, "ok", n_locations=len(locations), n_accounts=len(accounts), report=report, **stats)
        if verbose:
            print(f"run {run_id}: {len(locations)} locations, {len(accounts)} accounts, "
                  f"{len(proposals)} findings -> {stats}")
        return run_id, stats
    except Exception as e:
        store.finish_run(run_id, "error", error=str(e))
        raise


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # non-zero exit so the scheduler marks the run failed
        print(f"pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
