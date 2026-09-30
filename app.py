"""Local review app.  python app.py  ->  http://127.0.0.1:5000

The ONLY place CRM writes happen, and only on an explicit Approve click."""
import os
import traceback

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from bellhaven_sync.crm import CRMClient, StaleProposal, apply_proposal
from bellhaven_sync.matcher import KIND_ORDER
from bellhaven_sync.store import Store

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "local-review-only")
store = Store()

KIND_LABELS = {
    "chow_new_account": ("CHOW - new account", "Parent change on an account with revenue AND open AR: old account preserved, new one created."),
    "reparent": ("Re-parent", "Parent change, no open billing: existing account moved."),
    "create_account": ("New location", "On the website, not in the CRM."),
    "mark_duplicate": ("Duplicate", "Extra CRM record for a facility that already has a surviving record."),
    "update_fields": ("Field fix", "Right parent, stale name/address/care type."),
    "sold_to_other_operator": ("Sold", "Under Bellhaven in CRM, off the website, another operator's record at the same address."),
    "not_on_website": ("Not on website", "Under Bellhaven in CRM, off the website, no other evidence."),
    "predecessor_parent_empty": ("Empty parent", "Acquired operator's parent account with no live facilities left."),
}


def reviewer():
    return request.form.get("reviewer") or os.environ.get("REVIEWER", "reviewer")


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    kind = request.args.get("kind")
    rows = store.list(None if status == "all" else status)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    rows.sort(key=lambda r: (KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else 99, r["title"]))
    return render_template("index.html", rows=rows, status=status, kind=kind, counts=store.counts(),
                           labels=KIND_LABELS, last_run=store.last_run())


@app.route("/p/<key>")
def detail(key):
    p = store.get(key)
    if not p:
        abort(404)
    return render_template("detail.html", p=p, pay=p["payload"], labels=KIND_LABELS)


@app.post("/p/<key>/approve")
def approve(key):
    p = store.get(key)
    if not p or p["status"] not in ("pending", "failed"):
        flash("Only pending or failed proposals can be approved.", "err")
        return redirect(url_for("detail", key=key))
    try:
        log = apply_proposal(CRMClient(), p["payload"], reviewer(),
                             created_id=p.get("created_account_id"),
                             on_created=lambda aid: store.set_created(key, aid))
        store.set_status(key, "applied", reviewer(), request.form.get("comment", ""), result=log)
        flash(f"Applied: {p['title']}", "ok")
    except StaleProposal as e:
        store.set_status(key, "stale", reviewer(), str(e))
        flash(f"Not applied - CRM changed since this was proposed: {e}", "err")
    except Exception as e:  # noqa: BLE001
        store.set_status(key, "failed", reviewer(), str(e), result={"error": str(e), "trace": traceback.format_exc()})
        flash(f"Failed: {e}", "err")
        return redirect(url_for("detail", key=key))
    return redirect(url_for("index", status=request.form.get("back", "pending")))


@app.post("/p/<key>/reject")
def reject(key):
    p = store.get(key)
    if not p or p["status"] not in ("pending", "failed", "stale"):
        flash("Only open proposals can be rejected.", "err")
        return redirect(url_for("detail", key=key))
    store.set_status(key, "rejected", reviewer(), request.form.get("comment", ""))
    flash(f"Rejected: {p['title']}", "ok")
    return redirect(url_for("index", status=request.form.get("back", "pending")))


@app.post("/run")
def run_now():
    from bellhaven_sync.pipeline import run
    try:
        run_id, stats = run(store)
        flash(f"Run {run_id} complete: {stats}", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Run failed: {e}", "err")
    return redirect(url_for("index"))


@app.route("/matches")
def matches():
    return render_template("matches.html", run=store.last_run())


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
