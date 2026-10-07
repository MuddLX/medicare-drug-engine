# Medicare drug engine — folder map

**Runs on Railway** (don't move these): `app/` (the engine), `startup.py` (downloads the databases from
Cloudflare R2 at boot), `Procfile`, `nixpacks.toml`, `requirements.txt`, `runtime.txt`.
`tests/` — run `python -m pytest tests` before every push.

## Databases (root — the engine and Railway expect them here)
| File | What | In R2 as |
|---|---|---|
| `medicare_mn.db` | plan year **2026** (live until the switch) | `medicare_mn.db` |
| `medicare_mn_2027.db` | plan year **2027** (built by `build_2027_db.py`) | uploaded as `medicare_mn.db` at the switch |
| `pbp_benefits.db` / `pbp_benefits_2027.db` | client-sheet benefits (MOOP, copays, dental…) | `pbp_benefits.db` |
| `providers_2027.db` | doctors/clinics: 2027 directories, health systems, agency confirmations | `providers_2027.db` |

## Tools you run (root)
- `build_2027_db.py` — builds `medicare_mn_2027.db` step by step (see the top of the file).
  Oct 15: `python build_2027_db.py cms source_data\cms_files\<October file>.zip` then
  `python build_2027_db.py estimate_prices source_data\cms_files\SPUF_2026_20260701.zip`.
- `build_pbp_db.py` — client-sheet benefits: `python build_pbp_db.py source_data\cms_2027\pbp-benefits-2027 medicare_mn_2027.db pbp_benefits_2027.db`
- `r2_files.py` — list / upload / delete the files Railway downloads (`python r2_files.py list`).

## Folders
- `formulary_2027/` — carrier drug-list PDFs → drug IDs (reader, RxNorm matcher, checks).
- `providers_2027/` — Medica 2027 directory reader, health-system map, `confirmations.csv` (weekly agency lists).
- `source_data/` — raw downloads, not in git: `cms_2027/` (Landscape, PBP), `cms_2026/` (2026 PBP, stars),
  `census/` (ZIP→county, city→county), `carrier_formularies_2027/` (carrier PDFs, Wellcare lists),
  `providers_2027/` (Medica directory PDF, agency AEP sheet), `cms_files/` (CMS quarterly/monthly zips).
- `_archive/` — retired scripts, old test PDFs and old source files (kept, not used, not in git).
- `_scratch/` — throwaway checks and test renders.

## Docs
`SOA_Drug_Run_Automation.md` (project doc), `README_REFRESH.md` (2026 quarterly refresh — superseded by
`build_2027_db.py`, to be rewritten), `FUTURE_IMPROVEMENTS.md`.
