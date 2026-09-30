# Bellhaven Sync

This project keeps Bellhaven's facility-to-parent links in the CRM matched to Bellhaven's own website. It runs daily as a read-only job, and it writes to the CRM only when a reviewer approves a change.

```
scrape site ──► snapshot CRM ──► match + classify ──► proposals (SQLite)
                                                           │
                                   review app (Flask) ◄────┘
                                          │ Approve only
                                          ▼
                                   CRM API (PATCH/POST)
```

## Run it

```bash
pip install -r requirements.txt
export CRM_TOKEN=...            # never committed; see .env.example
python -m pytest -q             # 7 tests: matcher, SOP rules, re-run safety, scraper parsing
python -m bellhaven_sync.pipeline   # read-only: queues proposals
python app.py                   # http://127.0.0.1:5000  ->  review, approve, reject
```

You can also start a run from the **Run pipeline now** button in the app. Every run saves the raw site and CRM data to `snapshots/`, so any decision can be audited later.

## Layout

| File | Role |
|---|---|
| `bellhaven_sync/scraper.py` | Walks `/communities?page=N` using the site's own "Page X / N" marker. It also collects community links from the **home page**, because Findlay is only linked there. It checks the count it found against the site's "N communities listed". |
| `bellhaven_sync/normalize.py` | Address, name, and care-type normalization (`Northwest Sylvania Avenue` = `NW Sylvania Ave`, `Pk` = `Pike`). |
| `bellhaven_sync/matcher.py` | Pure logic, no network. Takes the website plus the CRM snapshot and returns proposals with evidence. |
| `bellhaven_sync/crm.py` | API client and `apply_proposal`, the **only** code that writes. |
| `bellhaven_sync/store.py` | Runs, proposals, and decisions. This is what makes re-runs safe. |
| `app.py`, `templates/` | The review app. |
| `.github/workflows/daily-sync.yml`, `crontab.txt` | Schedule config. |
| `tests/` | A fake CRM seeded from the real sandbox snapshot, with the same validation rules as the live API. |

## Matching

1. **Address first.** A normalized street plus zip, or a normalized street plus city and state (this catches the Portsmouth zip typo). Names change when facilities are acquired or rebranded; street addresses almost never do.
2. **Fallback, only when the CRM record has no usable address** (blank or a PO Box): same city, plus the same name core or the website's administrator listed as a CRM contact. This is how Ashtabula (CRM address `PO Box 517`) links up.
3. **Conflicting evidence blocks a match.** A record in the same city with a similar name but a *different physical address* is reported as a near miss. It is never linked automatically. See *Union Square* below.
4. **Chains are resolved.** An account whose `chow_current_account` or `duplicate_of_account` is set is represented by the record it points to. After a CHOW, the next run matches the new account and never flags the old one again.
5. **One survivor per facility.** When several records share an address, the survivor is picked in this order: has billing history, is already under Bellhaven, has the website's administrator as a contact, has the exact street string, has any parent, has more contacts. The other records get `duplicate_of_account` set to the survivor and are marked `Inactive`.

The website is treated as the source of truth for name, address, and care type. Phone mismatches are shown as evidence but **not** written. Nothing indicates which phone number is right, and a wrong number is worse than a stale one.

## Classification and what gets written

| Case | Write |
|---|---|
| Confident match, nothing differs | nothing |
| Right parent, stale name, address, or care type | PATCH those fields |
| Wrong parent, and **not** (revenue > 0 AND AR > 0) | PATCH `parent_id` (+ field fixes) + note |
| Wrong parent, revenue > 0 AND AR > 0 (**SOP**) | POST a new account under Bellhaven; PATCH **only** `chow_current_account` on the old account. The old account's name, parent, status, and note stay untouched. |
| On the website, not in the CRM | POST a new account under Bellhaven + note |
| Extra record for the same facility | `duplicate_of_account` = survivor, `status=Inactive`, note |
| Under Bellhaven in the CRM, off the website, and another operator's record exists at the address | Sold. If the account has revenue AND AR: `chow_current_account` → the existing new-owner record (no second record is created). Otherwise: duplicate of that record + Inactive. |
| Under Bellhaven in the CRM, off the website, no other evidence | `status=Needs Review` + note. **Not** Inactive: missing from a marketing site doesn't prove the facility closed or was sold. |
| An acquired operator's parent account ends up with no live children | `Needs Review` + note. This surfaces on the run *after* the moves are approved. |

Null or blank `lifetime_revenue` and `outstanding_ar` are treated as 0. The SOP condition is a strict AND.

## What this run found (sandbox)

35 website locations (34 in the directory plus Findlay, linked only from the home page), 121 CRM accounts, 29 first-run proposals:

- **CHOW (2):** Tiffin (rev 84,000 / AR 12,400) and Marietta (51,250 / 3,800), both under Cedar Trail.
- **Re-parent (4):** Lima (from Harborview; it has revenue but AR is 0, so it moves directly), Findlay (no parent), Zanesville (Cedar Trail, renamed), Kettering (Harborview, renamed).
- **New (4):** Batavia; Carlisle, PA (not the same place as New Carlisle, OH); Amberly Manor in Hudson, OH (the CRM's Amberly Manor is in Colorado Springs); Union Square.
- **Duplicates (7):** Owosso ×2, Port Clinton, Erie, Monroe ×3, Kettering ×3.
- **Field fixes (9):** 4 formatting renames (Grove City, Sycamore Ridge, Arbors, Ashland); 3 rebrands with the same address and the matching administrator (Riverbend Manor → Chagrin Falls, Sunny Acres → Willow Creek, Chesterton Senior Commons → Bellhaven of Chesterton); Ashtabula's PO Box replaced with the street address; Portsmouth zip 45626 → 45662.
- **Sold (1):** Bellhaven of Sandusky. It's off the website, and *Millstone Care of Sandusky* sits at the same address. The account has revenue + AR, so it is preserved and its `chow_current_account` is pointed at Millstone's existing record.
- **Not on website (2):** Alliance and Coldwater, flagged Needs Review.
- **Run 2:** Harborview and Cedar Trail parents flagged as empty (Needs Review). **Run 3:** 0 new proposals, and every website location resolves to a Bellhaven account with nothing to change (checked by `test_end_state_and_idempotent_reruns`).

### Judgment calls a reviewer should know about

- **Union Square.** The CRM has *Union Square Senior Living* in New Albany under Juniper Point, but at 240 Market St. The website lists 118 Union Square Dr, with a different administrator. I treated the address conflict as evidence they are different facilities and proposed a new account. The rejected candidate is shown on the proposal as a near miss.
- **Kettering survivor.** Three records, none with billing history. The survivor is the Harborview record, because it has the exact street and a parent (Harborview was acquired by Bellhaven in 2025, per the About page).
- **Sandusky.** The SOP says to "create a new account under the correct parent." A Millstone account already exists at that address, so creating another would add a duplicate. I pointed the CHOW at the existing record instead.

## Re-run safety

- Each proposal has a fingerprint: kind + subject + target field values. Dated notes are excluded from it. An applied or rejected fingerprint is never proposed again.
- Pending proposals that a later run no longer produces are marked `superseded`, because the data changed or someone fixed it by hand.
- **On approval, every target record is re-fetched and compared with the values the proposal was built from.** If anything has drifted, the proposal is marked `stale` and nothing is written.
- CHOW is two API calls. The new account id is saved the moment the POST succeeds, so a retry after a failed PATCH reuses that account instead of creating a second one.
- Notes are appended, never overwritten.
- If the scrape returns fewer than 80% of last run's communities, the run aborts. A broken scraper should not flag 30 facilities as "not on website".

## Scheduling

`crontab.txt` is the preferred setup: it runs daily at 6:15 AM ET on the same machine as the review app, so both use the same `sync.db`. The GitHub Actions workflow is the hosted alternative. It carries `sync.db` between runs with `actions/cache`. In production I'd put the proposal store in a shared database.

## Not done / next

- Contacts on retired duplicate records (e.g. Owosso's Tricia Lindqvist) are not moved to the survivor record. The contacts API supports it; it's the next thing I'd add, as its own proposal type.
- Administrator mismatches (e.g. Saline: the website says Dale Green, the CRM says Carla Castellano) are shown as evidence only.
- Bulk approve is deliberately left out.

## Time

Actual time: ~2.5 hours total (across two sessions)
- ~30 min: scoping the problem, exploring the site and CRM API, directing the build (Claude drafted the code)
- ~45 min: local setup on Windows, running tests, catching and fixing a Windows-specific test failure
- ~1 hr: reviewing all 31 proposals against the live CRM, deciding the Union Square case, approving, re-running to 0 new
- ~20 min: README, GitHub, submission

Built with Claude. I set requirements and constraints, verified the SOP cases (Tiffin, Marietta, Lima, Sandusky) against the CRM browser, made the judgment calls, and approved every change.
