# Medicare drug engine — folder map
*Updated October 7, 2026.*

**Runs on Railway** (don't move these): `app/` (the engine), `startup.py` (downloads the databases from
Cloudflare R2 at boot and checks them), `Procfile`, `nixpacks.toml`, `requirements.txt`, `runtime.txt`.
`tests/` — run `python -m pytest tests` before every push (808 tests on Oct 7).

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
