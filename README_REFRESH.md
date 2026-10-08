# Data refresh & go-live routine — 2027 plan year
*Rewritten October 7, 2026. Replaces the May 2026 quarterly-refresh guide (kept as `_archive/README_REFRESH_2026-05.md`;
`quarterly_refresh.py` is retired).*

Everything here is run by Jordon in **PowerShell, from the engine folder** (`C:\Users\Mudd\medicare_drug_engine`),
because the steps that download need internet. Each step says what "done" looks like.

---

## 1. How the live data works (read once)

Railway does not keep databases. **Every time the engine starts, `startup.py` downloads three files from
Cloudflare R2** (bucket `medicare-db`), checks them, and only then starts the engine:

| Name in R2 (what Railway loads) | What it is | Built from (local file) |
|---|---|---|
| `medicare_mn.db` | plans, drug lists, tier costs, prices, pharmacy networks, ZIP→county | **`medicare_mn_2027.db`** (since Oct 7, 2026) |
| `pbp_benefits.db` | client-sheet benefits + star ratings | **`pbp_benefits_2027.db`** (since Oct 7, 2026) |
| `providers_2027.db` | doctors/clinics: 2027 directories, health systems, agency confirmations | `providers_2027.db` |

Backups of the 2026 versions are in R2 as `backup_2026/medicare_mn.db` and `backup_2026/pbp_benefits.db`.

**Local names vs live names.** On your computer `medicare_mn.db` is still the **2026** database (the tests use it
because it holds CMS's official 2026 drug lists). The live 2027 data is built in `medicare_mn_2027.db` and
uploaded *under the name* `medicare_mn.db`. Always upload with `--as`:
```
python r2_files.py upload medicare_mn_2027.db --as medicare_mn.db
```

**Uploading alone changes nothing.** Railway only reads R2 when it restarts. After an upload, either push a
commit (`git push` → Railway redeploys) or in the Railway dashboard: service → **Deployments** → latest → **⋯ → Redeploy**.

**The database decides the year.** `medicare_mn_2027.db` carries a `meta` table (data year 2027, Part D cap
$2,400, data label, "prices are estimates" yes/no) and the 2027 Medicare-negotiated prices. The engine reads all
of that from the database — no code change at a switch.

---

## 2. The standard upload (every refresh below ends with this)

1. **Tests first:** `python -m pytest tests` → all pass (808 on Oct 7). Don't upload on a failure.
2. **Back up what's live** (only when replacing a file you haven't backed up this month):
   `python r2_files.py upload medicare_mn_2027.db --as backup_2027/medicare_mn_<YYYYMMDD>.db`
   (use the copy you built *before* today's change — or skip if `backup_2027/` already has last week's).
3. **Upload:** `python r2_files.py upload medicare_mn_2027.db --as medicare_mn.db` (and/or `pbp_benefits_2027.db --as pbp_benefits.db`, `providers_2027.db`).
4. **Check it's there:** `python r2_files.py list` → sizes/dates updated (2027 drug DB ≈ 41 MB).
5. **Restart Railway:** `git push` if you have commits, otherwise Redeploy in the dashboard.
6. **Railway logs** must show, in order: `medicare_mn.db validation passed: 46 plans` (count may change),
   `providers_2027.db validation passed`, `pbp_benefits.db validation passed: 37 plan benefit rows`, `Startup complete`.
7. **Health:** open `https://web-production-dcce3.up.railway.app/health` → `"data_year": 2027` and the
   `data_vintage` you expect; `"prices_estimated"` true until January's prices are loaded.
8. **One test client** through Roundabout (any test email from the chat) → both PDFs open, header shows the new data label.

**Rollback (2 minutes):** upload the backup under the live name, then Redeploy:
```
python r2_files.py upload <the backup file you kept locally> --as medicare_mn.db
```
(R2 backups can't be copied server-side with `r2_files.py`; keep the local backup file too. The 2026 files are
still local: `medicare_mn.db` / `pbp_benefits.db`.)

---

## 3. The calendar

| When | What | Section |
|---|---|---|
| **Weekly (AEP)** | Agency provider confirmations → `providers_2027.db` | 4 |
| **~Oct 9–10, 2026** | CMS posts **2027 Star Ratings** → client-sheet stars | 5 |
| **Oct 15, 2026** | CMS monthly file with **2027 drug lists + pharmacy networks** | 6 |
| Mid-Nov, mid-Dec | Newer monthly files (optional reload — same steps as 6) | 6 |
| **Monthly** | RxNorm drug names (the engine's drug-name reader) | 7 |
| **Jan 20, 2027** | CMS quarterly file with **real 2027 drug prices** (estimates end) | 8 |
| Apr / Jul / Oct 2027 | Quarterly files (prices + lists) | 8 |
| Before AEP 2028 | Build the 2028 database the same way | 9 |

CMS release schedule (monthly PUF ≈ mid-month; quarterly SPUF ≈ 3rd week of Jan/Apr/Jul/Oct). Monthly files have
lists, tier costs and networks but **no prices**; quarterly files add prices.

---

## 4. Weekly: agency provider confirmations
Agents send lists like "Dr. Smith, Allina Coon Rapids — UHC — in network". Jordon gives them to Claude, who adds
rows to `providers_2027/confirmations.csv`
(`provider,clinic,system,carrier,plans,status,confirmed_by,confirmed_on,note`) and rebuilds:
```
python providers_2027\build_systems.py
python -m pytest tests
python r2_files.py upload providers_2027.db
```
then Redeploy. A confirmation beats every other source on the report ("In network" / "Out of network",
with who confirmed it and when). **No client names** go in the file — only provider, plan, status.

## 5. 2027 Star Ratings (once, when CMS posts them)
1. Check https://www.cms.gov/medicare/health-drug-plans/part-c-d-performance-data — a link
   **"2027 Star Ratings Data Tables (ZIP)"** appears in the 2027 section.
2. Download it, unzip into `source_data\cms_2027\Star Ratings\` (the builder finds the file named
   `… Summary Ratings ….xlsx` anywhere under `source_data` and uses the **newest year**; it prints which file it used).
3. Rebuild benefits: `python build_pbp_db.py source_data\cms_2027\pbp-benefits-2027 medicare_mn_2027.db pbp_benefits_2027.db`
   → the line `Star ratings from: …2027…` must name the 2027 file.
4. Standard upload of `pbp_benefits_2027.db --as pbp_benefits.db`.
Until this is done the client sheet shows **2026** stars — no client sheets go out before it.

## 6. Oct 15: 2027 drug lists and pharmacy networks (monthly PUF)
1. Download the **Monthly Prescription Drug Plan Formulary and Pharmacy Network Information** file dated
   Oct 2026 from data.cms.gov into `source_data\cms_files\` (a zip of zips, several GB).
2. Keep a local backup: `copy medicare_mn_2027.db _scratch\medicare_mn_2027.before_oct15.db`
3. Load it (refuses a file that isn't for 2027 and changes nothing):
   `python build_2027_db.py cms source_data\cms_files\<October file>.zip`
   - First run extracts the inner zips to `source_data\cms_files\_work\` (slow once, fast after).
   - Plans CMS doesn't have yet keep their carrier drug lists; plans whose carrier hasn't published stay
     "Drug list not out yet".
4. Re-attach price estimates to the new drug lists:
   `python build_2027_db.py estimate_prices source_data\cms_files\SPUF_2026_20260701.zip`
5. Spot-check with Claude: a few test clients vs Medicare.gov Plan Finder (tiers should now match exactly).
6. Standard upload (section 2).

## 7. Monthly: RxNorm drug names
The engine reads drug names against a local copy of RxNorm (`app\data\rxnorm_concepts.json.gz`). New drugs
launched after the copy fall back to the online lookup, so refresh monthly:
```
python formulary_2027\download_rxnorm.py
python -m pytest tests
git add app/data/rxnorm_concepts.json.gz
git commit -m "RxNorm refresh <month>"
git push
```
The tests include ~345 written-out drug names (`tests\data\drug_cases*.psv`); all must pass. When a real sheet
has a drug the engine missed, add that line to `drug_cases.psv` first, then fix.

## 8. Jan 20, 2027 (and every quarter): real prices
1. Download the **Quarterly Prescription Drug Plan Formulary, Pharmacy Network, and Pricing Information** file
   (Q4 2026 release, ~Jan 20, 2027) into `source_data\cms_files\`.
2. `python build_2027_db.py cms source_data\cms_files\<quarterly file>.zip`
   → must print `prices: N rows from this file`. The database's "prices are estimates" flag turns off by itself
   and the report stops saying "estimate".
3. Standard upload. From here on, each quarterly file is the same two lines (1–2).

## 9. A new plan year (2028)
Same path as 2027 (all in `build_2027_db.py`; copy it to `build_2028_db.py` and change the year/file names):
Landscape → `zip_county`, `service_area`, `plans`; PBP → `costs`, benefits (`build_pbp_db.py`); `meta`
(cap, negotiated prices for the year); carrier drug lists or the October CMS file; estimates from the prior year's
prices; then the switch (section 2) when ready. Provider sources for the new year replace `providers_2027`.

---

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Railway won't start, log says `has only N plans` | Startup floor is 30 plans. A smaller file = a broken build; roll back. |
| `disk I/O error` building a database | A leftover journal file. The builders use `journal_mode=TRUNCATE`; close other programs using the file and rerun. |
| `This file has contract years [...]` | You loaded a file for the wrong plan year. Nothing changed. |
| Report still shows old data after upload | Railway wasn't restarted (Redeploy), or the upload used the wrong `--as` name (`r2_files.py list`). |
| A drug "couldn't identify" | Add it to `tests\data\drug_cases.psv`, then ask Claude to fix the reader. |
| `/health` `data_year` wrong | The wrong file is live under `medicare_mn.db` — check `r2_files.py list` sizes, re-upload, Redeploy. |
