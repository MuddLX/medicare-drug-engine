# Medicare drug engine — folder map
*Updated October 8, 2026.*

**Runs on Railway** (don't move these): `app/` (the engine), `startup.py` (downloads the databases from
Cloudflare R2 at boot and checks them), `Procfile`, `nixpacks.toml`, `requirements.txt`, `runtime.txt`.
`tests/` — run `python -m pytest tests` before every push (847 tests on Oct 8).


## ★ Added October 31, 2026
`tests/` now holds **902** tests (Oct 29).
| File / folder | What |
|---|---|
| `tools/mpf_load.py` | **loads Medicare.gov captures**: rebuilds `Plan jsons\_consolidated\mpf_prices_2027.db` (via `Plan jsons\_tools\mpf_consolidate.py`), matches new labels to RxNorm by reproducing Medicare.gov's copays, updates `app/data/mpf_drug_map.json`, runs `build_2027_db.py mpf_prices`. `--dry-run` changes nothing. Env `PLAN_JSONS`, `BUILD_2027_DB`. Backups → `_scratch\medicare_mn_2027.before_load_<date>.db` |
| `tools/verify_against_mgov.py` | **accuracy check**: re-runs every saved Medicare.gov search through the real engine path and compares yearly cost + deductible month (`--report`; results in `_scratch\verify_mgov\`) |
| `app/data/mpf_drug_map.json` | Medicare.gov drug label → RxNorm IDs (330 labels; commit after every load) |
| `tests/data/mgov_cap_cases.json` | Medicare.gov month-by-month tables used by the $2,400-cap regression test |
| `app/drug_year.py` | now also: Medicare.gov's yearly method, standard deductible ($700 in 2027) for the cap rule (`STD_CAP_RULE`) |
| `app/main.py` | also: `request_drugs` (structured drug list), `mpf_price_key` (chain or `npi:<NPI>`), `cheapest_pharmacy_year`, `hidden_contracts` (`HIDDEN_CONTRACTS`) |
| `build_2027_db.py` | new steps: `cms <monthly file>` (Oct 15 lists/networks), `mpf_prices <db>` (Medicare.gov prices; verified chains, store-level NPIs, one amount per drug) |

**Outside the engine folder (Jordon's computer):**
- `Desktop\Plan jsons\` — every saved Medicare.gov plan page (`2027_*\*.json` + `.txt` page text), `_tools\mpf_consolidate.py`, `_consolidated\mpf_prices_2027.db`, `labels_to_review.json`.
- `Desktop\Med Search 6\` — the capture scripts: `medicare_drug_fill.py`, `medicare_monthly_route.py`, `Drug_Fill_Plan.xlsx`, `run_drug_fill.bat` (README.txt there).
- `M:\Roundabout` — the desktop app (Claude Code only), v0.3.1.

## Engine code (`app/`)
| File | What |
|---|---|
| `main.py` | Flask endpoints, plan eligibility, county choice, drug costs, provider check wiring |
| `drug_resolver.py` | **drug names → RxNorm IDs**, offline (brand/generic, devices, ER/XL, abbreviations, spelling) |
| `rxnorm_core.py` | RxNorm name parsing shared by the resolver and the carrier-list matcher |
| `data/rxnorm_concepts.json.gz` | local RxNorm copy (refresh monthly: README_REFRESH §7) |
| `drug_year.py` | the year walked month by month: one shared deductible + the yearly out-of-pocket cap |
| `data_meta.py` | year facts read from the database (`meta` table: data year, cap, labels, estimates flag) |
| `providers_2027.py` | doctor/clinic status per plan from `providers_2027.db` |
| `internal_pdf.py` / `client_pdf.py` / `client_comparison.py` / `client_benefits.py` | the two reports |
| `compare_plans.py` | `POST /compare-plans` — every plan in a ZIP, priced, for Roundabout's Plans page |
| `availability.py` | **official-data gates** (prices / networks / carrier lists / stars) + each plan's official 2027 cost share per tier |

## Claude (since Oct 8, 2026)
The engine reaches Claude **only through AWS Bedrock** (`_claude_reply` in `main.py`; Sonnet 4.6, us-east-2), set by
`AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BEDROCK_MODEL_ID` (Railway variables; locally `.env`,
gitignored). `GET /health` → `claude` shows whether it's set up. Checks: `_scratch\bedrock_check_oldendpoint.py
<model>` (key + PDF reading), `_scratch\bedrock_normalize_check.py` (name clean-up locally),
`_scratch\live_check_bedrock.py` (Railway, 3 runs). `_scratch\bedrock_check.py <model>` tests the newer
bedrock-mantle entrance (Sonnet 5.5 — blocked for the account as of Oct 8).

## Databases (root — the engine and Railway expect them here)
| File | What | In R2 as |
|---|---|---|
| `medicare_mn_2027.db` | plan year **2027** (built by `build_2027_db.py`) — **LIVE since Oct 7, 2026** | `medicare_mn.db` |
| `medicare_mn.db` | plan year 2026 (CMS official lists; used by tests; rollback copy) | `backup_2026/medicare_mn.db` |
| `pbp_benefits_2027.db` / `pbp_benefits.db` | client-sheet benefits + stars, 2027 (live) / 2026 | `pbp_benefits.db` / `backup_2026/pbp_benefits.db` |
| `providers_2027.db` | doctors/clinics: Medica 2027 directory, health systems, agency confirmations | `providers_2027.db` |

## Tools you run (root) — full routine in `README_REFRESH.md`
- `build_2027_db.py` — builds `medicare_mn_2027.db` step by step (`cms`, `estimate_prices`, `carrier_formularies`, … — see the top of the file).
- `build_pbp_db.py` — client-sheet benefits + star ratings.
- `r2_files.py` — list / upload (`--as <name>`) / delete the files Railway downloads.
- `tools/drug_lookup_benchmark.py` — old (RxNav) vs new drug-name lookup on the test lists (needs internet).

## Folders
- `n8n_nodes/` — code for n8n Code nodes kept under version control (`build_bedrock_body_6a.js` = the reading
  prompt + request packing for node 6a). Edit here, then paste into n8n.
- `test_sheets/` — fake client sheets for end-to-end tests (`Fake Client Sheet - Testy McTestface.pdf`). Fake data only.
- `formulary_2027/` — carrier drug-list PDFs → drug IDs (reader, RxNorm matcher, checks); `download_rxnorm.py`.
- `providers_2027/` — Medica 2027 directory reader, health-system map, `confirmations.csv` (weekly agency lists).
- `tests/data/` — `drug_cases.psv` + `drug_cases_holdout.psv`: ~345 drug names as people write them, with answers.
- `source_data/` — raw downloads, not in git: `cms_2027/` (Landscape, PBP, stars), `cms_2026/`, `census/`,
  `carrier_formularies_2027/`, `providers_2027/`, `cms_files/` (CMS monthly/quarterly zips + `_work/`).
- `_archive/` — retired scripts, old docs, old test PDFs and source files (kept, not used, not in git).
- `_scratch/` — throwaway checks, test renders, local database backups.

## Docs
`SOA_Drug_Run_Automation.md` (master project doc — start at its ⭐ October 7 box), `README_REFRESH.md`
(data refresh & go-live routine), `FUTURE_IMPROVEMENTS.md` (go-live blockers, known gaps, next work).
