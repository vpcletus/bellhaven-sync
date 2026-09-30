"""SQLite store for runs, proposals, and reviewer decisions. This is what makes re-runs safe."""
from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("SYNC_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "sync.db"))

DECIDED = ("approved", "applied", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, status TEXT,
  n_locations INTEGER, n_accounts INTEGER,
  n_new INTEGER, n_kept INTEGER, n_skipped_decided INTEGER, n_superseded INTEGER,
  report TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  key TEXT PRIMARY KEY,
  kind TEXT, title TEXT, subject TEXT, account_id TEXT, slug TEXT,
  payload TEXT,
  status TEXT,               -- pending | applied | rejected | failed | stale | superseded
  first_run INTEGER, last_run INTEGER,
  created_at TEXT, decided_at TEXT, reviewer TEXT, review_comment TEXT,
  created_account_id TEXT,   -- set as soon as a create succeeds (makes retries idempotent)
  result TEXT
);
"""


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path=DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def start_run(self):
        cur = self.db.execute("INSERT INTO runs(started_at, status) VALUES (?, 'running')", (now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id, status, **fields):
        fields = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE runs SET finished_at=?, status=?{', ' + sets if sets else ''} WHERE id=?",
                        (now(), status, *fields.values(), run_id))
        self.db.commit()

    def upsert_proposals(self, run_id, proposals):
        """Insert new findings, keep open ones, never resurrect decided ones.
        Open proposals from earlier runs that were NOT regenerated are superseded
        (the underlying data changed, or someone fixed it by hand)."""
        stats = {"n_new": 0, "n_kept": 0, "n_skipped_decided": 0, "n_superseded": 0}
        seen = set()
        for p in proposals:
            seen.add(p["key"])
            row = self.db.execute("SELECT status FROM proposals WHERE key=?", (p["key"],)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO proposals(key, kind, title, subject, account_id, slug, payload, status, first_run, "
                    "last_run, created_at) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                    (p["key"], p["kind"], p["title"], p["subject"], p["account_id"], p["slug"], json.dumps(p),
                     run_id, run_id, now()))
                stats["n_new"] += 1
            elif row["status"] in DECIDED:
                stats["n_skipped_decided"] += 1
            else:  # pending / failed / stale -> refresh evidence, keep status (stale -> pending)
                self.db.execute("UPDATE proposals SET payload=?, last_run=?, status=CASE WHEN status='stale' "
                                "THEN 'pending' ELSE status END WHERE key=?", (json.dumps(p), run_id, p["key"]))
                stats["n_kept"] += 1
        open_rows = self.db.execute("SELECT key FROM proposals WHERE status IN ('pending','failed','stale')").fetchall()
        for r in open_rows:
            if r["key"] not in seen:
                self.db.execute("UPDATE proposals SET status='superseded', decided_at=? WHERE key=?", (now(), r["key"]))
                stats["n_superseded"] += 1
        self.db.commit()
        return stats

    def list(self, status=None):
        q = "SELECT * FROM proposals"
        args = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        rows = self.db.execute(q + " ORDER BY first_run, rowid", args).fetchall()
        return [self._row(r) for r in rows]

    def get(self, key):
        r = self.db.execute("SELECT * FROM proposals WHERE key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d

    def set_status(self, key, status, reviewer="", comment="", result=None):
        self.db.execute("UPDATE proposals SET status=?, decided_at=?, reviewer=?, review_comment=?, result=? WHERE key=?",
                        (status, now(), reviewer, comment, json.dumps(result) if result is not None else None, key))
        self.db.commit()

    def set_created(self, key, account_id):
        self.db.execute("UPDATE proposals SET created_account_id=? WHERE key=?", (account_id, key))
        self.db.commit()

    def counts(self):
        return {r["status"]: r["n"] for r in
                self.db.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status").fetchall()}

    def last_run(self):
        r = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["report"] = json.loads(d["report"]) if d.get("report") else []
        return d
