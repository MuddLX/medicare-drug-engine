# Medicare DB Quarterly Refresh Guide

Run this every time CMS releases new data — approximately January, April, July, and October.
The full process takes **30–45 minutes** (mostly waiting for geocoding).
Your actual hands-on time is about **5 minutes**.

---

## When to Run This

CMS releases new quarterly data on these approximate dates:
- **January 28** → run refresh in early February
- **April 22** → run refresh in late April
- **July 29** → run refresh in early August
- **October 15** → run refresh in late October **(also includes new plan year — most important refresh)**

Sign up for CMS email notifications at `https://www.cms.gov/subscribe` (subscribe to Medicare Plan Payment or Part D updates) so you get notified automatically.

---

## Step 1 — Download the CMS Files (5 minutes)

You need two files every quarter:

### File 1: SPUF Quarterly Zip (formulary, pricing, pharmacy network)
1. Go to: `https://www.cms.gov/research-statistics-data-systems/prescription-drug-plan-formulary-pharmacy-network-and-pricing-information-files`
2. Scroll to the Downloads section
3. Download the most recent **Quarterly** zip file (filename starts with `SPUF_`)
4. It will be several gigabytes — give it time to download

### File 2: Landscape CSV (premiums, deductibles, plan names)
1. Go to: `https://www.cms.gov/medicare/health-drug-plans/medicareadvtgspecialpolicies/benchmarks-payment-rates`
2. Look for the **Medicare Advantage and Part D Landscape Files** section
3. Download the current year Landscape file (filename looks like `CY2026_Landscape_YYYYMM.csv`)
4. This file is small (~5MB) and downloads quickly

> **Note:** In October, both files will reflect the NEW plan year (e.g. 2027 data). This is normal — run the refresh the same way.

---

## Step 2 — Drop Files into the Refresh Folder

Copy both downloaded files into:
```
C:\Users\Mudd\medicare_drug_engine\refresh_input\
```

You do **not** need to rename them. The script detects them automatically.

The folder should look like this (filenames will vary by quarter):
```
refresh_input/
├── SPUF_2026_20260422.zip
└── CY2026_Landscape_202604.csv
```

---

## Step 3 — Set Up Environment Variables (one-time setup only)

The script needs these variables to upload to Cloudflare R2 and send you notifications.

Create a file called `.env` in `C:\Users\Mudd\medicare_drug_engine\` with this content:

```
R2_ACCESS_KEY_ID=your_access_key_here
R2_SECRET_ACCESS_KEY=your_secret_key_here
R2_ENDPOINT_URL=https://your_account_id.r2.cloudflarestorage.com
R2_BUCKET_NAME=medicare-db
PUSHOVER_TOKEN=your_pushover_app_token
PUSHOVER_USER=your_pushover_user_key
RAILWAY_SERVICE_URL=https://web-production-dcce3.up.railway.app
```

> **You only do this once.** After setup, just skip to Step 4 every quarter.

---

## Step 4 — Run the Refresh Script

Open PowerShell, navigate to the project folder, and run:

```powershell
cd C:\Users\Mudd\medicare_drug_engine
python quarterly_refresh.py
```

Then **leave it running**. It will:
1. Detect your input files automatically
2. Extract the SPUF zip
3. Build a fresh database
4. Look up pharmacy names (NPI registry)
5. Geocode pharmacy addresses (Census API — this is the slow part)
6. Build zip→county mappings
7. Validate the new database
8. Upload to Cloudflare R2
9. Push to GitHub (triggers Railway redeploy)
10. Verify Railway came back up with the new data
11. Run a test SOA to confirm everything works
12. Send you a Pushover notification with the results

---

## What Success Looks Like

You'll see output like this at the end:
```
[14:32:01] INFO: === REFRESH COMPLETE in 38 minutes ===
[14:32:01] INFO:   Railway: live | Smoke test: passed
```

And you'll get a Pushover notification:
```
✅ Refresh complete (2026 Q2)
Time: 38 min
Railway: live
Smoke test: passed
```

---

## What to Do If Something Goes Wrong

### "No SPUF zip file found in refresh_input/"
You forgot to copy the files in, or they're in the wrong folder. Check `refresh_input/`.

### "Could not find these SPUF files in zip: pricing"
CMS changed the file structure inside the zip. Contact Jordon — the script needs an update.

### "Database validation failed"
The script found a problem with the data quality. Read the FAIL messages carefully.
Do NOT manually push anything — the live database was not changed.

### "Git push failed"
The new DB was built and uploaded to R2, but GitHub/Railway wasn't updated.
Run manually:
```powershell
cd C:\Users\Mudd\medicare_drug_engine
git add medicare_mn.db
git commit -m "Manual DB refresh"
git push
```

### "Railway did not redeploy within 5 minutes"
Check the Railway dashboard for deploy errors. The DB on R2 is correct — Railway just needs a kick.
Go to Railway → your service → Deployments → click Redeploy.

### Script crashed halfway through
Safe to re-run. The script builds to a temp DB and only replaces the live DB at the very end.
Running it again will skip pharmacies that are already geocoded (incremental).

---

## Backup and Recovery

The script automatically:
- Saves a local backup at `medicare_mn_backup.db` before replacing the live DB
- Saves a timestamped backup to Cloudflare R2 at `backups/medicare_mn_2026_Q2.db`

To roll back to the previous DB:
```powershell
cd C:\Users\Mudd\medicare_drug_engine
copy medicare_mn_backup.db medicare_mn.db
git add medicare_mn.db
git commit -m "Rollback to previous DB"
git push
```

---

## Files in This Project

| File | What it does |
|------|-------------|
| `quarterly_refresh.py` | Master refresh script — run this |
| `validate_db.py` | DB quality checks (run standalone anytime: `python validate_db.py`) |
| `startup.py` | Railway startup — downloads DB from R2 automatically |
| `medicare_mn.db` | The live database |
| `medicare_mn_backup.db` | Previous DB backup (created each refresh) |
| `refresh_input/` | Drop your CMS files here |
| `.env` | Your credentials (never commit this to git) |

---

## Quarterly Refresh Checklist

Copy this and use it each quarter:

```
[ ] CMS email received — new files available
[ ] Downloaded SPUF zip from CMS
[ ] Downloaded Landscape CSV from CMS
[ ] Copied both files to refresh_input/
[ ] Ran: python quarterly_refresh.py
[ ] Received Pushover: ✅ Refresh complete
[ ] Opened a test SOA in Make to verify end-to-end
[ ] Deleted old files from refresh_input/
```

---

*Last updated: May 2026*
