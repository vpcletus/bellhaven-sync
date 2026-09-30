"""Matching + classification. Pure logic: (website locations, CRM snapshot) -> proposals.

Nothing in this module talks to the network. That makes it testable against a
fixture snapshot and keeps the "propose" step strictly read-only.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .normalize import (crm_care_types, digits, is_po_box, name_core, name_similarity,
                        norm_city, norm_street, norm_zip, person)

TOOL = "bellhaven-sync"

# Proposal kinds
UPDATE = "update_fields"          # right parent, some field stale (name/address/care type)
REPARENT = "reparent"             # wrong parent, no open billing -> move directly (+ field fixes)
CHOW = "chow_new_account"         # wrong parent, revenue AND AR > 0 -> new account + chow pointer
CREATE = "create_account"         # on website, not in CRM
DUPLICATE = "mark_duplicate"      # extra CRM record for the same facility
SOLD = "sold_to_other_operator"   # under Bellhaven, gone from website, another operator's record at same address
NOT_ON_SITE = "not_on_website"    # under Bellhaven, gone from website, no other evidence
PARENT_EMPTY = "predecessor_parent_empty"

KIND_ORDER = [CHOW, REPARENT, CREATE, DUPLICATE, UPDATE, SOLD, NOT_ON_SITE, PARENT_EMPTY]


def money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def has_open_billing(acct) -> bool:
    """SOP: preserve the old account only when BOTH lifetime revenue and outstanding AR are > 0.
    Null / blank / non-numeric are treated as 0."""
    return money(acct.get("lifetime_revenue")) > 0 and money(acct.get("outstanding_ar")) > 0


def is_parent_account(a) -> bool:
    return "(parent account)" in (a.get("name") or "").lower() or (
        not a.get("billing_street") and not a.get("parent_id"))


def proposal_key(kind, subject, actions) -> str:
    """Stable fingerprint of WHAT is being proposed (notes excluded, since they carry dates).
    Same finding on a later run -> same key -> not re-proposed once decided."""
    stripped = []
    for act in actions:
        f = {k: v for k, v in (act.get("fields") or {}).items() if k != "note"}
        stripped.append({"op": act["op"], "account_id": act.get("account_id"), "fields": f})
    raw = json.dumps([kind, subject, stripped], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class Matcher:
    def __init__(self, locations, accounts, contacts, parent_id, site_url=""):
        self.locations = locations
        self.accounts = {a["account_id"]: a for a in accounts}
        self.parent_id = parent_id
        self.site_url = site_url.rstrip("/")
        self.admins = defaultdict(set)      # account_id -> {normalized admin names}
        self.contacts_by_acct = defaultdict(list)
        for c in contacts:
            if c.get("is_active", True) is False:
                continue
            self.contacts_by_acct[c["account_id"]].append(c)
            if "administrator" in (c.get("title") or "").lower():
                self.admins[c["account_id"]].add(person(c["name"]))
        self.facilities = [a for a in accounts if not is_parent_account(a)]
        self.by_street_zip = defaultdict(list)
        self.by_street_city = defaultdict(list)
        for a in self.facilities:
            s = norm_street(a.get("billing_street"))
            if s:
                self.by_street_zip[(s, norm_zip(a.get("billing_zip")))].append(a)
                self.by_street_city[(s, norm_city(a.get("billing_city")),
                                     (a.get("billing_state") or "").upper())].append(a)

    # ---------- helpers ----------
    def pname(self, pid):
        return (self.accounts.get(pid) or {}).get("name", "(none)") if pid else "(no parent)"

    def resolve(self, acct):
        """Follow chow_current_account / duplicate_of_account to the record that is current."""
        seen = set()
        while acct and acct["account_id"] not in seen:
            seen.add(acct["account_id"])
            nxt = acct.get("chow_current_account") or acct.get("duplicate_of_account")
            if not nxt or nxt not in self.accounts:
                return acct
            acct = self.accounts[nxt]
        return acct

    def admin_match(self, acct, loc):
        return bool(loc.get("administrator")) and person(loc["administrator"]) in self.admins[acct["account_id"]]

    def site_link(self, loc):
        return f"{self.site_url}/communities/{loc['slug']}" if self.site_url else loc["slug"]

    def acct_view(self, a):
        keys = ["account_id", "name", "parent_id", "billing_street", "billing_city", "billing_state",
                "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
                "chow_current_account", "duplicate_of_account", "note"]
        v = {k: a.get(k) for k in keys}
        v["parent_name"] = self.pname(a.get("parent_id"))
        v["contacts"] = [f"{c['name']} ({c.get('title') or '-'})" for c in self.contacts_by_acct[a["account_id"]]]
        return v

    def new_account_fields(self, loc):
        care = crm_care_types(loc.get("offerings", []))
        return {
            "name": loc["name"], "parent_id": self.parent_id,
            "billing_street": loc["street"], "billing_city": loc["city"],
            "billing_state": loc["state"], "billing_zip": loc["zip"],
            "phone": loc.get("phone", ""), "care_type": care[0] if care else "",
            "status": "Active",
        }

    # ---------- candidate search ----------
    def find_candidates(self, loc):
        s = norm_street(loc["street"])
        hits = {a["account_id"]: a for a in self.by_street_zip.get((s, norm_zip(loc["zip"])), [])}
        # same street, same city/state, zip differs -> likely a zip typo in CRM
        for a in self.by_street_city.get((s, norm_city(loc["city"]), loc["state"].upper()), []):
            hits.setdefault(a["account_id"], a)
        method = {aid: "address" for aid in hits}
        near_misses = []
        # Fallback for records with no usable physical address (blank or PO Box): same city
        # + same name core (or same administrator). A record with a DIFFERENT physical
        # address is conflicting evidence, so it is only reported as a near miss.
        core = name_core(loc["name"])
        for a in self.facilities:
            if a["account_id"] in hits:
                continue
            same_city = (norm_city(a.get("billing_city")) == norm_city(loc["city"])
                         and (a.get("billing_state") or "").upper() == loc["state"].upper())
            if not same_city:
                continue
            name_hit = core and name_core(a["name"]) == core
            admin_hit = self.admin_match(a, loc)
            if not (name_hit or admin_hit):
                continue
            street = a.get("billing_street") or ""
            if not street or is_po_box(street):
                hits[a["account_id"]] = a
                method[a["account_id"]] = "name+city" + ("+administrator" if admin_hit else "") + " (CRM has no street address)"
            else:
                near_misses.append({"account_id": a["account_id"], "name": a["name"],
                                    "street": street, "parent": self.pname(a.get("parent_id")),
                                    "why_not": "same city and similar name/administrator but a different physical address"})
        # Collapse chains: an account that already points (chow/duplicate) elsewhere is represented
        # by its current record.
        resolved = {}
        for aid, a in hits.items():
            cur = self.resolve(a)
            resolved.setdefault(cur["account_id"], (cur, method[aid]))
        return list(resolved.values()), near_misses

    def survivor_rank(self, loc):
        def key(pair):
            a, _ = pair
            return (
                0 if (money(a.get("lifetime_revenue")) > 0 or money(a.get("outstanding_ar")) > 0) else 1,
                0 if a.get("parent_id") == self.parent_id else 1,
                0 if self.admin_match(a, loc) else 1,
                0 if (a.get("billing_street") or "").strip().lower() == loc["street"].strip().lower() else 1,
                0 if a.get("parent_id") else 1,
                -len(self.contacts_by_acct[a["account_id"]]),
                -name_similarity(a["name"], loc["name"]),
                a["account_id"],
            )
        return key

    # ---------- main ----------
    def run(self):
        proposals, report = [], []
        matched_ids = set()

        for loc in self.locations:
            cands, near = self.find_candidates(loc)
            ev_site = {"website": {**{k: loc.get(k) for k in ("name", "street", "city", "state", "zip",
                                                             "administrator", "phone")},
                                   "offerings": loc.get("offerings", []), "url": self.site_link(loc)}}
            if not cands:
                fields = self.new_account_fields(loc)
                fields["note"] = f"Created by {TOOL}: listed on Bellhaven website ({self.site_link(loc)}); no matching CRM account."
                actions = [{"op": "create", "ref": "new", "fields": fields}]
                why = "No CRM account at this address (or a no-address record with the same name/administrator in this city)."
                if near:
                    why += " Near misses were checked and rejected because their physical address conflicts."
                proposals.append(self._p(CREATE, loc["slug"], None, loc, f"Create account: {loc['name']} ({loc['city']}, {loc['state']})",
                                         why, actions, {**ev_site, "near_misses": near}))
                report.append({"slug": loc["slug"], "name": loc["name"], "classification": "new", "account_id": None})
                continue

            cands.sort(key=self.survivor_rank(loc))
            survivor, how = cands[0]
            matched_ids.update(c["account_id"] for c, _ in cands)
            classification = []

            # Duplicates
            for dup, dup_how in cands[1:]:
                fields = {"duplicate_of_account": survivor["account_id"], "status": "Inactive",
                          "note": f"{TOOL}: duplicate of {survivor['account_id']} ({survivor['name']}) - same facility "
                                  f"as website listing {self.site_link(loc)}."}
                actions = [{"op": "patch", "account_id": dup["account_id"], "fields": fields,
                            "expect": {"duplicate_of_account": dup.get("duplicate_of_account") or "",
                                       "chow_current_account": dup.get("chow_current_account") or ""}}]
                why = (f"Same facility as {survivor['name']} ({survivor['account_id']}), matched by {dup_how}. "
                       f"Survivor chosen by: billing history > already under Bellhaven > administrator matches website "
                       f"> exact street > has a parent > more contacts.")
                if has_open_billing(dup):
                    why += " WARNING: this duplicate has revenue and open AR - billing should confirm before approving."
                proposals.append(self._p(DUPLICATE, dup["account_id"], dup, loc,
                                         f"Mark duplicate: {dup['name']} -> {survivor['name']}", why, actions,
                                         {**ev_site, "survivor": self.acct_view(survivor), "match_method": dup_how}))
                classification.append("duplicate")

            # Desired state for the survivor
            diffs = {}
            if survivor.get("name") != loc["name"]:
                diffs["name"] = loc["name"]
            crm_addr_ok = (norm_street(survivor.get("billing_street")) == norm_street(loc["street"])
                           and norm_zip(survivor.get("billing_zip")) == norm_zip(loc["zip"])
                           and norm_city(survivor.get("billing_city")) == norm_city(loc["city"])
                           and (survivor.get("billing_state") or "").upper() == loc["state"].upper())
            if not crm_addr_ok:
                diffs.update({"billing_street": loc["street"], "billing_city": loc["city"],
                              "billing_state": loc["state"], "billing_zip": loc["zip"]})
            wanted_care = crm_care_types(loc.get("offerings", []))
            if wanted_care and survivor.get("care_type") not in wanted_care:
                diffs["care_type"] = wanted_care[0]
            # drop no-op fields
            diffs = {k: v for k, v in diffs.items() if (survivor.get(k) or "") != v}

            ev = {**ev_site, "crm": self.acct_view(survivor), "match_method": how,
                  "administrator_matches_crm_contact": self.admin_match(survivor, loc)}
            if digits(survivor.get("phone")) != digits(loc.get("phone")):
                ev["phone_mismatch_not_changed"] = {"crm": survivor.get("phone"), "website": loc.get("phone")}

            if survivor.get("parent_id") != self.parent_id:
                old_parent = self.pname(survivor.get("parent_id"))
                billing = (f"lifetime_revenue={money(survivor.get('lifetime_revenue')):,.0f}, "
                           f"outstanding_ar={money(survivor.get('outstanding_ar')):,.0f}")
                if has_open_billing(survivor):
                    fields = self.new_account_fields(loc)
                    fields["note"] = (f"{TOOL}: change of ownership. Facility moved from {old_parent} to Bellhaven. "
                                      f"Prior account {survivor['account_id']} preserved for billing ({billing}).")
                    actions = [
                        {"op": "create", "ref": "new", "fields": fields},
                        {"op": "patch", "account_id": survivor["account_id"],
                         "fields": {"chow_current_account": "$new"},
                         "expect": {"parent_id": survivor.get("parent_id") or "",
                                    "chow_current_account": survivor.get("chow_current_account") or ""}},
                    ]
                    why = (f"Listed on the Bellhaven website but parented to {old_parent}. Account has revenue history AND "
                           f"open AR ({billing}), so per SOP the old account is left untouched, a new account is "
                           f"created under Bellhaven, and the old account's chow_current_account points to it.")
                    proposals.append(self._p(CHOW, survivor["account_id"], survivor, loc,
                                             f"CHOW: {survivor['name']} ({old_parent} -> Bellhaven)", why, actions, ev))
                    classification.append("chow")
                else:
                    fields = {"parent_id": self.parent_id, **diffs,
                              "note": f"{TOOL}: re-parented from {old_parent} to Bellhaven per website "
                                      f"{self.site_link(loc)}."}
                    actions = [{"op": "patch", "account_id": survivor["account_id"], "fields": fields,
                                "expect": {"parent_id": survivor.get("parent_id") or "",
                                           **{k: survivor.get(k) or "" for k in diffs}}}]
                    why = (f"Listed on the Bellhaven website but parented to {old_parent}. No open billing ({billing}; "
                           f"SOP requires revenue AND AR > 0 to preserve), so the existing account is re-parented directly"
                           + (f" and {', '.join(k for k in diffs)} synced to the website." if diffs else "."))
                    proposals.append(self._p(REPARENT, survivor["account_id"], survivor, loc,
                                             f"Re-parent: {survivor['name']} ({old_parent} -> Bellhaven)", why, actions, ev))
                    classification.append("reparent")
            elif diffs:
                fields = dict(diffs)
                actions = [{"op": "patch", "account_id": survivor["account_id"], "fields": fields,
                            "expect": {k: survivor.get(k) or "" for k in diffs}}]
                why = "Correct parent; " + ", ".join(sorted(diffs)) + " differ from the website (website treated as source of truth)."
                if "name" in diffs and name_similarity(survivor["name"], loc["name"]) < 0.6:
                    why += f" Name change looks like a rebrand ('{survivor['name']}' -> '{loc['name']}'); matched by {how}."
                proposals.append(self._p(UPDATE, survivor["account_id"], survivor, loc,
                                         f"Update: {survivor['name']}" + (f" -> {loc['name']}" if "name" in diffs else ""),
                                         why, actions, ev))
                classification.append("update")
            else:
                classification.append("confirmed")
            report.append({"slug": loc["slug"], "name": loc["name"], "classification": "+".join(classification),
                           "account_id": survivor["account_id"]})

        # ---------- Bellhaven children that are no longer on the website ----------
        for a in self.facilities:
            if a.get("parent_id") != self.parent_id or a["account_id"] in matched_ids:
                continue
            if a.get("chow_current_account") or a.get("duplicate_of_account"):
                continue  # already resolved to another record
            s = norm_street(a.get("billing_street"))
            others = [o for o in self.by_street_zip.get((s, norm_zip(a.get("billing_zip"))), [])
                      if o["account_id"] != a["account_id"] and o.get("parent_id") != self.parent_id
                      and o.get("status") != "Inactive" and not o.get("duplicate_of_account")]
            ev = {"crm": self.acct_view(a), "website": "not listed on any Bellhaven community page"}
            if others:
                other = others[0]
                ev["other_operator_record"] = self.acct_view(other)
                billing = (f"lifetime_revenue={money(a.get('lifetime_revenue')):,.0f}, "
                           f"outstanding_ar={money(a.get('outstanding_ar')):,.0f}")
                if has_open_billing(a):
                    actions = [{"op": "patch", "account_id": a["account_id"],
                                "fields": {"chow_current_account": other["account_id"]},
                                "expect": {"parent_id": a.get("parent_id") or "", "chow_current_account": ""}}]
                    why = (f"No longer on the Bellhaven website, and {other['name']} under {self.pname(other.get('parent_id'))} "
                           f"exists at the same address: the facility changed hands. Account has revenue AND open AR "
                           f"({billing}), so per SOP it is left untouched except chow_current_account -> the existing "
                           f"new-owner account (no second account is created because one already exists).")
                else:
                    actions = [{"op": "patch", "account_id": a["account_id"],
                                "fields": {"duplicate_of_account": other["account_id"], "status": "Inactive",
                                           "note": f"{TOOL}: facility now operated by {self.pname(other.get('parent_id'))}; "
                                                   f"current record is {other['account_id']}."},
                                "expect": {"parent_id": a.get("parent_id") or "", "duplicate_of_account": ""}}]
                    why = (f"No longer on the Bellhaven website; {other['name']} under {self.pname(other.get('parent_id'))} "
                           f"already represents this address. No open billing ({billing}), so this record is retired as a "
                           f"duplicate of the new owner's record.")
                proposals.append(self._p(SOLD, a["account_id"], a, None, f"Sold/transferred: {a['name']} -> {other['name']}",
                                         why, actions, ev))
            else:
                if a.get("status") == "Needs Review":
                    continue
                actions = [{"op": "patch", "account_id": a["account_id"],
                            "fields": {"status": "Needs Review",
                                       "note": f"{TOOL}: under Bellhaven in CRM but not listed on the Bellhaven website. "
                                               f"Possibly sold or closed - verify before changing parent or deactivating."},
                            "expect": {"status": a.get("status") or ""}}]
                why = ("Parented to Bellhaven but absent from the website, with no other CRM record at the address. "
                       "Absence from a marketing site is not proof of closure or sale, so this flags it for a human "
                       "(Needs Review) instead of deactivating or re-parenting.")
                proposals.append(self._p(NOT_ON_SITE, a["account_id"], a, None, f"Not on website: {a['name']}", why, actions, ev))

        # ---------- predecessor parents left with no live facilities ----------
        children = defaultdict(list)
        for a in self.facilities:
            if a.get("parent_id"):
                children[a["parent_id"]].append(a)
        for pid, kids in children.items():
            p = self.accounts.get(pid)
            if not p or pid == self.parent_id or p.get("status") == "Needs Review":
                continue
            live = [k for k in kids if k.get("status") != "Inactive"
                    and not k.get("chow_current_account") and not k.get("duplicate_of_account")]
            moved_to_bh = [k for k in kids if self.resolve(k).get("parent_id") == self.parent_id
                           and k.get("parent_id") != self.parent_id]
            if live or not moved_to_bh:
                continue
            actions = [{"op": "patch", "account_id": pid,
                        "fields": {"status": "Needs Review",
                                   "note": f"{TOOL}: all facilities under this parent in the CRM now resolve to Bellhaven "
                                           f"(re-parented, CHOW'd, or duplicates). Confirm whether this operator still exists."},
                        "expect": {"status": p.get("status") or ""}}]
            why = (f"Every child of {p['name']} is now inactive, a duplicate, or CHOW'd to a Bellhaven account "
                   f"({len(moved_to_bh)} moved). Flagged, not deactivated: the parent may still operate facilities we don't sell to.")
            proposals.append(self._p(PARENT_EMPTY, pid, p, None, f"Parent now empty: {p['name']}", why, actions,
                                     {"crm": self.acct_view(p), "children": [self.acct_view(k) for k in kids]}))

        proposals.sort(key=lambda p: (KIND_ORDER.index(p["kind"]), p["title"]))
        return proposals, report

    def _p(self, kind, subject, acct, loc, title, why, actions, evidence):
        for act in actions:  # snapshot of current values, for the reviewer's before/after diff
            if act["op"] == "patch" and act["account_id"] in self.accounts:
                cur = self.accounts[act["account_id"]]
                act["before"] = {k: cur.get(k) or "" for k in act["fields"]}
        return {
            "key": proposal_key(kind, subject, actions),
            "kind": kind,
            "subject": subject,
            "account_id": acct["account_id"] if acct else None,
            "slug": loc["slug"] if loc else None,
            "title": title,
            "rationale": why,
            "actions": actions,
            "evidence": evidence,
        }
