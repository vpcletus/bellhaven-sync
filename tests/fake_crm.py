"""In-memory stand-in for the CRM API, loaded from the real snapshot, enforcing the same
validation rules observed on the live API (mutable fields, status enum, reference checks)."""
import copy
import itertools
import json
import os

from bellhaven_sync.crm import CRMError

HERE = os.path.dirname(__file__)
MUTABLE = {'name', 'parent_id', 'status', 'note', 'care_type', 'phone', 'billing_street', 'billing_city',
           'billing_state', 'billing_zip', 'chow_current_account', 'duplicate_of_account'}
COLS = ["account_id", "name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip",
        "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar"]


def load(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return json.load(f)


def fixture_locations():
    keys = ["slug", "name", "street", "city", "state", "zip", "offerings", "administrator", "phone"]
    return [dict(zip(keys, row)) for row in load("site.json")]


class FakeCRM:
    _ids = itertools.count(1)

    def __init__(self):
        self.accounts = {}
        for row in load("accounts.json"):
            a = dict(zip(COLS, row))
            a.update(chow_current_account="", duplicate_of_account="", note="")
            self.accounts[a["account_id"]] = a
        self.contacts = [{"account_id": a, "name": n, "title": t, "is_active": True} for a, n, t in load("contacts.json")]
        self.calls = []

    def list_accounts(self, **_):
        return copy.deepcopy(list(self.accounts.values()))

    def list_contacts(self, **_):
        return copy.deepcopy(self.contacts)

    def get_account(self, aid):
        if aid not in self.accounts:
            raise CRMError(f"404 {aid}")
        return copy.deepcopy(self.accounts[aid])

    def _validate(self, fields):
        if fields.get("status") and fields["status"] not in ("Active", "Inactive", "Needs Review"):
            raise CRMError("bad status")
        for ref in ("parent_id", "chow_current_account", "duplicate_of_account"):
            if fields.get(ref) and fields[ref] not in self.accounts:
                raise CRMError(f"{ref} must reference an existing account_id")

    def create_account(self, fields):
        if not fields.get("name"):
            raise CRMError("name is required")
        self._validate(fields)
        aid = f"NEW{next(self._ids):05d}"
        a = {c: "" for c in COLS}
        a.update(lifetime_revenue=0, outstanding_ar=0, chow_current_account="", duplicate_of_account="", note="")
        a.update({k: v for k, v in fields.items() if k in MUTABLE})
        a["account_id"] = aid
        self.accounts[aid] = a
        self.calls.append(("POST", aid, fields))
        return copy.deepcopy(a)

    def patch_account(self, aid, fields):
        f = {k: v for k, v in fields.items() if k in MUTABLE}
        if not f:
            raise CRMError("No mutable fields provided")
        self._validate(f)
        self.accounts[aid].update(f)
        self.calls.append(("PATCH", aid, f))
        return copy.deepcopy(self.accounts[aid])
