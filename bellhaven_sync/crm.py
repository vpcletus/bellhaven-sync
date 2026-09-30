"""Thin CRM API client + the only code path that writes to the CRM (apply_proposal)."""
from __future__ import annotations

import os
import time

import requests

BASE_URL = os.environ.get("CRM_BASE_URL", "https://analyst-assessment-production.up.railway.app/api/v1")


class CRMError(Exception):
    pass


class StaleProposal(Exception):
    """CRM changed since the proposal was generated; nothing was written."""


class CRMClient:
    def __init__(self, token: str | None = None, base_url: str = BASE_URL, timeout=30):
        token = token or os.environ.get("CRM_TOKEN")
        if not token:
            raise CRMError("CRM_TOKEN environment variable is not set")
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.timeout = timeout

    def _req(self, method, path, **kw):
        url = f"{self.base}{path}"
        for attempt in range(4):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise CRMError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500 or r.status_code == 429:
                if attempt == 3:
                    raise CRMError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise CRMError(f"{method} {path}: HTTP {r.status_code} {r.text[:500]}")
            return r.json()

    def _all(self, path, **params):
        out, page = [], 1
        while True:
            body = self._req("GET", path, params={**params, "page": page, "page_size": 100})
            data = body.get("data", [])
            out.extend(data)
            if not data or len(out) >= body.get("total", 0):
                return out
            page += 1

    def list_accounts(self, **filters):
        return self._all("/accounts", **filters)

    def list_contacts(self, **filters):
        return self._all("/contacts", **filters)

    def get_account(self, account_id):
        return self._req("GET", f"/accounts/{account_id}")

    def create_account(self, fields):
        return self._req("POST", "/accounts", json=fields)

    def patch_account(self, account_id, fields):
        return self._req("PATCH", f"/accounts/{account_id}", json=fields)


def find_parent_id(accounts, name_fragment="bellhaven"):
    hits = [a for a in accounts if name_fragment in (a.get("name") or "").lower()
            and "(parent account)" in a["name"].lower()]
    if len(hits) != 1:
        raise CRMError(f"Expected exactly one '{name_fragment}' parent account, found {len(hits)}: "
                       f"{[h['name'] for h in hits]}")
    return hits[0]["account_id"]


def _merge_note(existing: str, new: str) -> str:
    existing = (existing or "").strip()
    if not new or new in existing:
        return existing
    return f"{existing}\n{new}" if existing else new


def apply_proposal(client: CRMClient, proposal: dict, reviewer: str = "", created_id: str | None = None,
                   on_created=None):
    """Execute an approved proposal. Returns a log of API calls.

    Safety:
      * Every patched record is re-fetched first and compared with the values the proposal was
        built from ("expect"). Any drift -> StaleProposal, and nothing is written.
      * A create that already happened (created_id passed back in after a partial failure) is
        reused, never repeated, so retrying a failed CHOW can't create a second account.
      * Notes are appended, never overwritten.
    """
    log = []
    today = time.strftime("%Y-%m-%d")
    # 1. pre-flight: verify nothing moved under us
    for act in proposal["actions"]:
        if act["op"] == "patch" and not act["account_id"].startswith("$"):
            current = client.get_account(act["account_id"])
            for field, before in (act.get("expect") or {}).items():
                if (current.get(field) or "") != (before or ""):
                    raise StaleProposal(f"{act['account_id']}.{field} is now {current.get(field)!r}, "
                                        f"proposal expected {before!r}. Re-run the pipeline.")
    # 2. execute
    refs = {}
    if created_id:
        refs["$new"] = created_id
    for act in proposal["actions"]:
        fields = dict(act.get("fields") or {})
        if "note" in fields:
            fields["note"] = f"[{today}{' ' + reviewer if reviewer else ''}] {fields['note']}"
        if act["op"] == "create":
            if "$new" in refs:
                log.append({"op": "create", "skipped": f"already created {refs['$new']}"})
                continue
            res = client.create_account(fields)
            refs["$new"] = res["account_id"]
            if on_created:
                on_created(res["account_id"])
            log.append({"op": "create", "account_id": res["account_id"], "fields": fields})
        elif act["op"] == "patch":
            aid = act["account_id"]
            fields = {k: refs.get(v, v) if isinstance(v, str) else v for k, v in fields.items()}
            if "note" in fields:
                current = client.get_account(aid)
                fields["note"] = _merge_note(current.get("note"), fields["note"])
            client.patch_account(aid, fields)
            log.append({"op": "patch", "account_id": aid, "fields": fields})
    return log
