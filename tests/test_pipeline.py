"""End-to-end tests against a fake CRM seeded from the real sandbox snapshot.
Run:  python -m pytest -q   (or: python tests/test_pipeline.py)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bellhaven_sync.crm import apply_proposal  # noqa: E402
from bellhaven_sync.pipeline import run  # noqa: E402
from bellhaven_sync.store import Store  # noqa: E402
from tests.fake_crm import FakeCRM, fixture_locations  # noqa: E402

BH = "0015QAPLGS3FVYEEEM"


def approve_all(store, crm):
    for p in store.list("pending"):
        log = apply_proposal(crm, p["payload"], "test",
                             on_created=lambda aid, k=p["key"]: store.set_created(k, aid))
        store.set_status(p["key"], "applied", "test", result=log)


def fresh():
    # In-memory DB per test: no temp file, so nothing for Windows to lock between tests.
    return Store(":memory:"), FakeCRM()


def by_kind(store):
    out = {}
    for p in store.list("pending"):
        out.setdefault(p["kind"], []).append(p)
    return out


def test_first_run_classification():
    store, crm = fresh()
    run(store, crm, fixture_locations(), verbose=False)
    k = by_kind(store)
    subj = {kind: sorted(p["subject"] for p in ps) for kind, ps in k.items()}
    # SOP: Tiffin + Marietta have revenue AND AR under Cedar Trail -> CHOW
    assert subj["chow_new_account"] == ["001A34WFSUYHCRBLFT", "001U6RW32TY0WSXZZB"]
    # Lima (rev, AR=0), Findlay (rev, AR=0, no parent), Zanesville, Kettering survivor -> direct reparent
    assert "001LGFPBJY4N9MB6KL" in subj["reparent"] and "001UKEFGADQ8YCZ4YM" in subj["reparent"]
    assert "001H1JMVZWP46D5VUF" in subj["reparent"]
    # New: Batavia, Carlisle PA, Amberly Manor (Hudson OH, not the Colorado one), Union Square (conflicting address)
    creates = sorted(p["slug"] for p in k["create_account"])
    assert creates == ["amberly-manor", "bellhaven-at-union-square", "bellhaven-of-batavia", "bellhaven-of-carlisle"]
    # Sandusky: Bellhaven account w/ rev+AR, Millstone record at same address -> chow pointer, no new account
    sold = k["sold_to_other_operator"][0]
    assert sold["subject"] == "001SXSF4ELF0Z2LGDM"
    assert sold["payload"]["actions"] == [a for a in sold["payload"]["actions"] if a["op"] == "patch"]
    assert sold["payload"]["actions"][0]["fields"] == {"chow_current_account": "0017JP8Z1UQ763BVK3"}
    assert sorted(p["subject"] for p in k["not_on_website"]) == ["00116ETS45BL7DTQP7", "0016PVXH4B25HWR7QE"]
    dups = {p["subject"]: p["payload"]["actions"][0]["fields"]["duplicate_of_account"] for p in k["mark_duplicate"]}
    assert dups["001QU150PM4Z15UA71"] == "001EGU7BMJ942ZTRE6"          # Owosso: admin-matching record survives
    assert dups["001JD2MWRA74LTSN24"] == "001UELXDAKFRKB8932"          # Port Clinton
    assert dups["001BLYF02K97SZLZHH"] == "001CVBBCSDM7YHN220"          # Erie
    assert dups["0011AB44D05WLA9HTX"] == dups["00159PL81N38KM4FHM"] == "001U1750VLVJAGG1S5"  # Monroe
    assert len([d for d in dups if dups[d] == "001WR41PYNWXCAE2X4"]) == 2  # Kettering: 3 records -> 1


def test_end_state_and_idempotent_reruns():
    store, crm = fresh()
    run(store, crm, fixture_locations(), verbose=False)
    approve_all(store, crm)
    # CHOW: old accounts untouched except the pointer; new account under Bellhaven
    tiffin_old = crm.accounts["001U6RW32TY0WSXZZB"]
    assert tiffin_old["parent_id"] == "001FWSQ30SFW6S7604" and tiffin_old["note"] == ""
    assert crm.accounts[tiffin_old["chow_current_account"]]["parent_id"] == BH
    assert crm.accounts["001LGFPBJY4N9MB6KL"]["parent_id"] == BH                 # Lima re-parented
    assert crm.accounts["001NXP9X46CWEPSLSV"]["billing_street"] == "3156 W Prospect Rd"  # PO box fixed
    assert crm.accounts["001CF3LDWVRGL09P4F"]["billing_zip"] == "45662"           # zip typo fixed
    assert crm.accounts["0017MN2JYAJBDS8WQZ"]["name"] == "Bellhaven Willow Creek"  # rebrand
    assert crm.accounts["001SXSF4ELF0Z2LGDM"]["parent_id"] == BH                  # Sandusky preserved as-is

    # Run 2: only the "predecessor parent now empty" findings appear (they depend on run-1 approvals)
    _, stats = run(store, crm, fixture_locations(), verbose=False)
    kinds = sorted(p["kind"] for p in store.list("pending"))
    assert kinds == ["predecessor_parent_empty", "predecessor_parent_empty"], kinds
    approve_all(store, crm)

    # Run 3: nothing new. Every location resolves to a correct Bellhaven account.
    _, stats = run(store, crm, fixture_locations(), verbose=False)
    assert stats["n_new"] == 0 and store.list("pending") == [], stats
    report = store.last_run()["report"]
    assert all(r["classification"] == "confirmed" for r in report), [r for r in report if r["classification"] != "confirmed"]
    for r in report:
        assert crm.accounts[r["account_id"]]["parent_id"] == BH


def test_rejected_items_are_not_reproposed():
    store, crm = fresh()
    run(store, crm, fixture_locations(), verbose=False)
    victim = store.list("pending")[0]
    store.set_status(victim["key"], "rejected", "test", "disagree")
    _, stats = run(store, crm, fixture_locations(), verbose=False)
    assert store.get(victim["key"])["status"] == "rejected"
    assert stats["n_new"] == 0 and stats["n_skipped_decided"] >= 1


def test_stale_proposal_writes_nothing():
    from bellhaven_sync.crm import StaleProposal
    store, crm = fresh()
    run(store, crm, fixture_locations(), verbose=False)
    p = next(p for p in store.list("pending") if p["kind"] == "reparent")
    crm.accounts[p["subject"]]["parent_id"] = "00139TNDS8HNLUZ5A6"  # someone moved it by hand meanwhile
    n = len(crm.calls)
    try:
        apply_proposal(crm, p["payload"])
        assert False, "should have raised"
    except StaleProposal:
        pass
    assert len(crm.calls) == n


def test_chow_retry_does_not_double_create():
    store, crm = fresh()
    run(store, crm, fixture_locations(), verbose=False)
    p = next(p for p in store.list("pending") if p["kind"] == "chow_new_account")
    created = []
    orig = crm.patch_account
    crm.patch_account = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network blip"))
    try:
        apply_proposal(crm, p["payload"], on_created=created.append)
    except RuntimeError:
        pass
    crm.patch_account = orig
    before = len(crm.accounts)
    apply_proposal(crm, p["payload"], created_id=created[0])
    assert len(crm.accounts) == before
    assert crm.accounts[p["subject"]]["chow_current_account"] == created[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
