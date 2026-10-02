# SOA Drug Run Automation — Complete Project Documentation
*Last updated: October 2, 2026*

> ⚠ **Read the CURRENT ARCHITECTURE section immediately below first — it is authoritative.** The detailed sections further down (What This Does, Architecture Overview, Node Map, etc.) are preserved reference from the pre-review-loop build (July 2026) and describe an earlier **single-pass** version of the pipeline. Where they differ from the Current Architecture section, this section wins. On 2026-09-10 this master doc absorbed the former standalone "CIS Review Loop (Phase 8)" and "Human-Readable Filenames (Option A)" build docs; those files were retired to keep the vault lean. On 2026-09-19 the **AEP Readiness Roadmap** was also merged into this doc (see the roadmap section below), so this is now the **single living document**. On 2026-09-23 all live project docs were consolidated into one vault folder, **`Projects/SOA Engine/`**: this master, the Phase 3 client-sheet doc (`Phase 3 — Client-Facing Plan Comparison PDF.md`), and its mockup. The used-up `Phase 3 — Pending Claude Code Prompts.md` moved to `Archive/`. The repo copy (`medicare_drug_engine\SOA_Drug_Run_Automation.md`) is still kept byte-identical to this vault master. **On 2026-10-02 the agent-chosen-plans architecture (decided with Jill Oct 1) was added — see the ⭐ October 2026 box at the top of Current Architecture and the OCTOBER 2026 UPDATE in the roadmap. It supersedes the system ranking plans and the high-confidence auto-report path.**

---

## ═══ CURRENT ARCHITECTURE (September 2026) ═══

*Live, authoritative description. Folds in the former Phase 8 review-loop and Human-Readable Filenames build docs.*

### ⭐ October 2026 — Agent-chosen plans (supersedes system plan ranking)
*Decided Oct 1, 2026 (Jordon + Jill). Engine side built + verified live Oct 2. App (Roundabout Phase 12) and n8n changes in progress. Where anything below this box describes the system picking plans, the high-confidence path generating reports automatically, or the resume workflow, this box wins.*
*Status end of Oct 2: **all of it is built, switched over and tested end to end** (app Phases 12–21, n8n extraction-only + claim-on-pickup, engine picker detail + one-table internal report). Details in the roadmap under "Oct 2, 2026 (afternoon)".*
*Status evening of Oct 2: hands-on testing with a deliberately messy fake sheet (Harold Lindgren) exposed how flagged fields reach the agent. Fixed on the engine and n8n side; app Phases 22–25 (layout + review polish) queued in Claude Code. Details in the roadmap under "Oct 2, 2026 (evening)".*

**The decision.** The system no longer chooses which plans go on either report. The **agent** chooses.
- After extraction, **every** sheet pauses for the agent, at high confidence too. The agent confirms or corrects the extraction, then picks plans.
- The app fetches **every plan available in the client's ZIP** and shows a picker: Medicare Advantage + standalone Part D only. **No Medigap, no Special Needs Plans.** Names only (e.g. "UnitedHealthcare — UHC MN-0001 (PPO)"), no costs, so the list doesn't steer the pick. *(Updated Oct 2, Jordon: names alone were too generic, so each plan now also shows its monthly premium, drug deductible, CMS plan number and official name — facts only; the list order stays carrier → plan name, no sorting by price, no "best" highlight.)*
- **Pick order = column order** on both reports.
- **One selection drives both PDFs.**
  - **Client sheet:** up to 4 columns = the first 3 Medicare Advantage picks + the first Part D pick (always the rightmost column).
  - **Internal report:** up to 7 Medicare Advantage + 3 Part D, in pick order.
- **Why:** agents know the client (doctors, preferences, what they're already on) better than any ranking rule, and an agent-made choice removes the "why did the system recommend this plan?" steering question. Engineering Principle 8 (AI recommends, humans decide) gets stronger: AI only reads and normalizes; a person decides.

**New flow:**
```
Agent uploads CIS (Roundabout) → n8n extracts (Claude) → review file → Needs Review (ALL sheets)
  → agent confirms/corrects in the app → app: GET /plans-for-zip → agent picks plans
  → app: POST /process-soa (internal report) + POST /client-comparison (client sheet),
    both with the same ordered selected_plans → app saves both PDFs
```
- **n8n now only extracts.** ✅ Done Oct 2. Planned change: the main workflow's `Route by Confidence (8)` true output goes to `Build Review JSON (19)`, same as the low path. The old high-confidence report nodes stay disconnected (not deleted) for rollback.
- **`CIS Resume - After Review` retires** (unpublished, kept for rollback). The app generates reports itself, so nothing needs to "resume."
- **Side effects to handle:** the Google Sheets SOA Tracker audit log was written by the n8n report paths, so it stops when they stop (decide: app-side log or drop). Handwritten "also check <plan>" margin notes (`custom_plans`) no longer drive anything; the picker replaces them.

**Engine (built Oct 2, commits `a446bee` + `bae6db1`, live on Railway):**
- `GET /plans-for-zip?zip=55441` returns `{zip_code, county, county_exact, data_vintage, plans:[{contract_id, plan_id, plan_type "MA"|"PD", carrier, plan_name, display_name}]}`. Medicare Advantage first, then Part D, each sorted by carrier then plan name. 400 for a bad ZIP, 404 when no county is found. `county_exact` is false when the ±3 neighbor-ZIP fallback was used.
- `POST /process-soa` and `POST /client-comparison` accept an optional ordered `selected_plans: [{"contract_id","plan_id"}, …]`. When present it replaces the old ranking. Validation returns a plain-English 400: plan not available in the county, the same plan picked twice, or over the limit (7 MA / 3 PD). When absent, both endpoints behave exactly as before (backward compatible).
- Same eligibility rules everywhere (one shared function): the picker can never show a plan the reports would reject.
- **Cost period fix:** the internal report used to price drugs from the SOA month to December (October–December during AEP) while the client sheet priced the full year. For a future plan year, it now prices **January–December**, so both reports show the same yearly drug cost. Current-year requests (mid-year enrollment) still use the remaining months.
- Part D columns on the client sheet: medical and extra-benefit rows say "Not included"; doctors "—"; star rating "—" (Part D star ratings aren't loaded).
- Carrier names come from the CMS contract number (`CONTRACT_CARRIER` in `client_comparison.py`), not word matching ("Allina Health Aetna Medicare" contains "medica").
- Internal report: Cost plans now show in Section 1 (they were silently hidden), and Section 2 says "Not identified" instead of "Not Covered" for drugs the engine couldn't identify.
- Tests: `tests/test_agent_selected_plans.py`, 22 offline tests (`python -m pytest tests`). Live check: `python _scratch\live_check_selected_plans.py` (fake client, saves both PDFs to `_scratch\`, 4 PASS/FAIL checks).

### What changed since the July 2026 build
The pipeline is no longer a single-pass "scan → report" flow. Three big things landed:
1. **Human-in-the-loop review loop (Phase 8)** — low-confidence extractions route to a review app instead of a dead-end folder; a human corrects them and the pipeline resumes.
2. **The Roundabout desktop app** — a Windows/PySide6 app is now the front-end: agents upload Client Information Sheets, review low-confidence ones, and retrieve finished reports through it (no more raw folder-diving).
3. **Human-Readable Filenames** — reports and archived originals are named for the client (e.g. `Arthur Pendleton - Plan Comparison 2027.pdf`), not raw ULID codes. The app matches files via a hidden ULID sidecar. *(This is the feature previously called "Option A" — that was never a real name, just "the first of two options I listed once." It is Human-Readable Filenames.)*

**Terminology:** the forms are now **Client Information Sheets (CIS)**, not SOA forms. "SOA" survives only as legacy naming in the workflow name, the Google Sheet, and the Railway endpoint.

### The four n8n workflows
Self-hosted n8n v2.19.5 (https://n8n.muddlx.com), all **path-based** (no folder IDs), triggered by a **Schedule Trigger** poll (the old flaky OneDrive Trigger was replaced):
1. **`SOA Automation Workflow`** (main, ~25 nodes) — polls `Incoming/`, extracts with Claude, routes by confidence (threshold **0.7**). High-confidence → generates the report + sidecar, archives the original, cleans up. Low-confidence → writes a review file to `Needs Review/` and stops.
2. **`CIS Engine - Report Back-Half`** (shared sub-workflow) — takes the extraction, calls the Railway report engine, returns the PDF binary. Called by BOTH the main and resume workflows so report logic lives in one place.
3. **`CIS Resume - After Review`** (~19 nodes) — polls for corrected files, regenerates the report + sidecar for a human-corrected sheet, archives, cleans up. Has an idempotency guard (skips if the sidecar already exists) and its own Failed-file error path.
4. **`CIS Pipeline - Error Handler`** — the global Error Workflow; emails on any unhandled failure.

> **Oct 2026:** with agent-chosen plans, #1 becomes extraction-only (every sheet → Needs Review), #2 is no longer called by the app path (the app calls Railway directly), and #3 (`CIS Resume`) retires. See the ⭐ October 2026 box above.

### The Roundabout app (front-end)
Windows desktop app (PySide6), local path `M:\Roundabout`, SQLite-backed, reads/writes the OneDrive workspace folders. It:
- Uploads a CIS (drag-drop or browse) → writes `<ULID>__cis.pdf` + `<ULID>__meta.json` to `Incoming/`.
- Shows each sheet's status (In Progress / Pending Review / Completed / Failed) by reconciling the workspace folders every few seconds (event-driven file watcher + backup poll; reads **local synced files only — no Graph API calls**).
- Lets a human correct a low-confidence sheet in-app → writes `<ULID>__corrected.json`, which the resume workflow picks up.
- Surfaces finished reports (View Report, Save a copy) via the sidecar.
See `M:\Roundabout\docs\` for the app's own architecture + the app↔pipeline contract.

### OneDrive workspace folders (current — workspace root is `Client Info Workflow/`)
```
Client Info Workflow/
├── Incoming/         ← app writes <ULID>__cis.pdf + <ULID>__meta.json; CamScanner scans also land here
├── Needs Review/     ← low-confidence: <ULID>__review.json (+ the CIS) for the app to show
├── Completed Review/ ← corrected sheets after resume (cleanup target)
├── Reports/          ← finished report (human-named) + hidden <ULID>__reportmeta.json sidecar
├── Archive/          ← human-named archived original CIS (permanent record, unique names)
└── Failed/           ← <ULID>__error.json for sheets that hard-failed
```
*(Folder renames Archive → "Completed Client Sheets" and Reports → "Plan Comparisons" are planned — see roadmap.)*

### Human-Readable Filenames + the ULID sidecar
- Report: `<Client Name> - Plan Comparison <planYear>.pdf` (planYear = 2027 for AEP; hardcoded, auto-flip planned).
- Archived original: `<Client Name> - Client Information Sheet <docYear>.pdf` (docYear = current calendar year).
- Sidecar `Reports/<ULID>__reportmeta.json` (hidden by the app after first sight) links the human-named report back to its ULID and carries the client name:
```json
{ "ulid": "…", "schema": 1, "client_name": "Arthur Pendleton",
  "report_filename": "Arthur Pendleton - Plan Comparison 2027.pdf",
  "created_at": "…" }
```
- Rules: report PDF written **first**, sidecar **last** (sidecar's presence guarantees the PDF); the app **waits for both** before marking complete; the app still matches legacy `<ULID>__report.pdf` files (backward compatible); `client_name` in the sidecar is what makes a high-confidence sheet show the client's name in the app.
- **Why a sidecar, not PDF metadata:** no Railway change, no new app dependency, and it preserves the resume idempotency guard. (Metadata would have needed the sidecar's work *plus* a Railway redeploy *plus* a PDF library.)

### File naming / de-duplication logic
Naming is **folder-based**: before naming, the workflow lists the actual `Reports/`/`Archive/` folder contents and picks the **lowest number not already taken** (empty → no number, one there → `(2)`, etc.). A deleted file frees its number, a re-upload comes back clean, and two different people with the same name get different numbers automatically — the number reflects the **folder**, not a counter. (This replaced an earlier scheme that counted append-only Google-Sheet rows, which never reset and drifted.)
> ✅ **Confirmed live on BOTH paths (Sept 18).** Folder-based naming is applied to the main workflow *and* the resume workflow (resume twin done). Verified: a client run 3× produced `(2)`/`(3)` in `Reports/` and `Archive/` with no overwrite/collision, and the Roundabout app shows the matching `(2)`/`(3)` suffix on completed rows (derived from the report filename, so app and folder always agree).

### Key operational facts
- Confidence threshold: **0.7**.
- Report engine: Railway Flask/Python app (`MuddLX/medicare-drug-engine`), ReportLab PDF, CMS/formulary + provider DBs in Cloudflare R2 (details preserved in the sections below).
- Audit log: Google Sheets SOA Tracker — **append-only; never use as a live counter**.
- Pipeline is **active** and running on its poll timer (the July doc's `active: false` is stale).
- Global error alerting exists (email via the Error Handler workflow); the remaining gap is an app-visible Failed-file writer on the **main** path — see roadmap.

### What's pending (full detail in the AEP Readiness Roadmap section below)
Confirm folder-based naming + resume twin, remove dead dedup nodes, main-path Failed-file writer, centralize naming/sidecar in the shared engine, clean SOA-era prompt fields, test-data reset, data-refresh hardening, and the future client-facing plan-comparison PDF.

> **Sept 23 update:** the client-facing plan-comparison PDF is **LIVE end-to-end on both paths** (front + resume) and polished; full detail in the Phase 3 doc in this folder. The go-live order is now locked: **core → compliance → folder restructure → final end-to-end tests → per-agent cloning**. See "Locked go-live order (Sept 23)" in the roadmap below.

---

## ═══ AEP READINESS ROADMAP ═══
*Created: August 27, 2026 · Owner: Jordon (MuddLX). Merged into this master doc on Sept 19, 2026 — the roadmap and the as-built doc are now a single living document.*

> Quick capture so this lives outside a chat. The plan for getting the **internal** drug-run engine sharpened and AEP-ready (PY2027, ~Oct 15 – Dec 7, 2026). Naming note: this is really a "client information" engine now, but we're keeping the SOA name since we're far enough in.

### ═══ OCTOBER 2026 UPDATE ═══
*Last updated: Oct 2, 2026. The September section below is preserved as history; this section is the live picture.*

#### Oct 1, 2026 — Architecture change: agents choose the plans (meeting with Jill)
Full design in the ⭐ October 2026 box at the top of Current Architecture. Jordon's decisions, in short:
- Build it **before the Saturday Oct 3 test with Lacey** (fake data only).
- **App-side generation:** the app calls the engine directly; n8n only extracts; the resume workflow retires.
- Picker lists all Medicare Advantage + standalone Part D plans for the ZIP; **no Medigap, no Special Needs Plans**; names only.
- One selection drives both PDFs; pick order = column order.
- Client sheet max 4 = first 3 MA + first Part D (rightmost; always include a Part D when one is picked). Internal report up to 7 MA + 3 PD.
- Division of work: Roundabout changes → **Claude Code (Phase 12, "Prompt B")**. Engine changes → done directly by Claude via the linked computer.

#### Oct 2, 2026 — Engine side built, tested, live
- `a446bee` — `/plans-for-zip` + `selected_plans` on both reports (details in the ⭐ box).
- `bae6db1` — internal report prices the full plan year for a future plan year (matched the client sheet: UHC MN-0001 went from $1,191 for Oct–Dec to $2,113.75, client sheet $2,114).
- Verified offline (22 tests; the new cost-period tests fail before the fix and pass after) and **live on Railway**: `/plans-for-zip?zip=55441` returned 28 Hennepin plans (21 MA + 7 PD), identical to local. `live_check_selected_plans.py` passed all 4 checks: client sheet (UHC, HealthPartners, Aetna, then Humana Part D on the right; the 4th MA pick, Medica, correctly left off), internal report (all 5 picks in order), fake plan → 400 "Plan H0000-001 is not available in Hennepin County", duplicate → 400 "Plan H2001-116 was selected more than once."
- Both reports still one page.

#### Oct 2, 2026 (late) — App + n8n switched over; first end-to-end test PASSED
- **Roundabout Phase 12 reviewed** (Claude Code; 928 tests). One gap fixed by Claude directly: the app's `/process-soa` request didn't send `plan_year`, so app-made internal reports would have priced only Oct–Dec. `src/engine/bodies.py` now sends it (required parameter); 3 tests fail without it and pass with it; app contract doc `03` updated. Exe rebuilt.
- **n8n switched (published):** `Route by Confidence (8)` true output → `Build Review JSON (19)`, so every sheet goes to Needs Review. Old high-confidence chain (26, 27, 11–18, 24, 28, 29) left disconnected for rollback. `CIS Resume - After Review` **unpublished** (kept for rollback). Publish note: version `Agent-chosen plans: every sheet → Needs Review`.
- **End-to-end test PASSED** (fake client "Frances Whitaker", ZIP 55441; picks Aetna Enhanced, Blue Cross Choice, HealthPartners Journey Pace + SilverScript Choice): same column order on both PDFs, yearly drug costs match (Aetna $1,670 / $1,670, Blue Cross $1,820.75 / $1,821, HealthPartners $1,770 / $1,770, SilverScript $2,154.79 / $2,155), internal report prices January–December. **Still to confirm:** a deliberate Details-tab edit carrying through to both PDFs.
- **Picker detail (engine, commit after `bae6db1`):** agents found names too generic, so `/plans-for-zip` now also returns `premium_monthly`, `drug_deductible`, `plan_number` (e.g. `H3219-002`) and `official_name` (e.g. "Allina Health Aetna Medicare Enhanced (PPO)"). Premium/deductible come from the same helper as the reports (`plan_premium_deductible`), so the picker can't disagree with the PDFs. 24 engine tests. **Needs a push** + the app side (below).
- **Roundabout Phase 13 (Claude Code, in progress):** "Pending Review" renamed **"Choose Plans"** and moved to the top of the sidebar (Jordon's picks); row tag "Ready" vs "N fields to check"; clean sheets open straight on the Plans tab; a read-only client/medication summary on the Plans tab so a confidently misread dose is still visible; "Processing" → "Reading the sheet". Follow-up for the same phase: show premium / deductible / plan number / official name in the picker.
- **Cleanup later (after the new flow is proven, incl. Lacey's test + a few days of real use):** export the main workflow, then delete the old high-confidence chain and `Route by Confidence (8)` (both outputs now go to the same place), and delete `CIS Resume - After Review`.
- **Noticed:** the "Form notes" banner still shows SOA-era noise ("Form appears to be a client intake sheet, not a standard Scope of Appointment form", "no dental coverage interest noted"). Fix in the n8n `Extract with Claude (6)` prompt (#9), ideally before Saturday. Also a cosmetic font-size glitch on some Section 3 pharmacy entries.
- **Tooling gotcha:** when writing files back to Jordon's computer, reusing an output file name can re-send an OLD cached copy (the write "succeeds" with stale content). Always use a fresh output file name per write and verify with a checksum/grep afterwards.

#### Oct 2, 2026 (afternoon) — Hardening, new layout built, new internal report
**Roundabout (Claude Code, Phases 13–21)** — all built, exe rebuilt after each:
- **13** "Choose Plans" naming, ready/flagged row tags, clean sheets open on the Plans tab, read-only client + medication summary on the Plans tab. **13+** picker shows premium / deductible / plan number / official name.
- **14** Fixed flashing mini-windows (parentless widgets shown before being added to a layout — ~27 stray windows per plan list). "Edit anyway" → "Save change" (Enter saves, Esc cancels, "Edited" marker, Submit blocked while a field is open). Auto-open when a sheet is ready. Duplicate data-vintage line removed.
- **15** Unsaved-change prompt (Save / Discard / Cancel) on every way of leaving a sheet; Details fields full width, long values wrap. **Standing build rule** in the app's `CLAUDE.md`: Claude Code closes Roundabout itself, rebuilds, reopens it (through Explorer, so it uses the real database).
- **16** New layout built (ahead of the "after Saturday" plan): **Upload · To Do · Completed Client Reports · Plans (coming soon)**. Backup of the previous exe kept as `dist\Roundabout_phase15_backup.exe`.
- **17** Freeze fix: the app froze ("Not responding") for 91 s right after a batch upload auto-opened a sheet. Log showed no error; most likely cause = the preview reading a file OneDrive was still downloading, on the UI thread. Preview + file opens moved to background threads ("Loading preview…"), a **stall watcher** (`src/stall_watch.py`) now logs exactly where the UI is stuck if it is silent > 5 s. Found: the copy that froze had been launched by Claude Code inside its sandbox with a private database copy. Auto-open now only for a **single** upload (batches get one "N sheets are ready — open To Do" toast). "Edit anyway" → "Edit"; grey "Edited" tag; no more "Syncing…" flicker; **smarter plan search** (every word, any order, plus BC/BCBS, HP, UHC/AARP, MBRx shorthand).
- **18–19** Colour rules: **blue = buttons only** (solid blue = the main action; light-blue filled = other buttons; dark mode deep-blue fill with near-white text; all ≥ 4.5:1 contrast). Selected rows/sidebar neutral with a blue edge bar. Duplicate clients explained (below). Sidebar wide enough for "Completed Client Reports".
- **20** Completed Client Reports polish: client card (one facts line: DOB / ZIP / county / reports; Medications and Doctors side by side as plain lists), report cards with date only (no time), "Latest" tag, "New" tag until opened, underlined "View original scan", compact buttons, old reports say "No client sheet — made before client sheets existed". A client with a new report jumps to the top.
- **21** Test-data reset (archive, not delete; database backed up first) + Help page now just says "contact Jordon" (no phone/email in the app).
- **Duplicate clients** were old test sheets with **no date of birth** (pre-Phase-12); the app deliberately never merges without a matching DOB. Real sheets always carry a DOB → one entry per client.
- **One exe for every agent:** the exe holds code only; each person's data lives in `%LOCALAPPDATA%\Roundabout` + their own OneDrive workspace. A new agent's first launch is empty and runs the setup wizard. No per-agent build needed.
- **Sprint test rule** (in `CLAUDE.md`): during a phase only the changed area's tests run (full suite ~6 min × several runs made phases take 45+ min); the **full suite runs for a "release build"** before any exe goes to an agent.
- Still open in the app: the routine folder check still runs on the UI thread (own phase later); **"Run again with different plans"** deferred (touches generation); Roundabout is **not under version control** (and the update check points at a GitHub repo that doesn't exist yet).

**n8n (main workflow `SOA Automation Workflow`)** — published as `Claim-on-pickup + 15s polling` and `Extraction prompt: Client Information Sheet + agent-relevant flags only`:
- Trigger every **15 seconds** (node still named `Every Minute (1)`).
- **Claim-on-pickup:** `Watch Incoming Folder (3)` picks the **oldest** unclaimed sheet; new `Claim File (30)` creates `Incoming/claim__<file id>.json` with Graph `conflictBehavior=fail` (only one run can create it); new `Claim Result (31)` Switch: Claimed → `Read ULID (4)`, 409 Already claimed → stop quietly, anything else → new `Claim Failed (32)` (Stop and Error → Error Handler email). Markers older than 10 min are treated as a crashed run and replaced. New `Delete Claim Marker (33)` (DELETE, On Error: Continue) after `Delete Meta Sidecar (25)` on both of its outputs. `Read ULID (4)` now reads node 3 directly. The app ignores `claim__` files (it only watches `*__cis.*`).
- Result: duplicates impossible; a batch is read in parallel (3 sheets ≈ 1 minute, verified).
- **Error Workflow was never set on this workflow** (no failure emails before today) → now set to `CIS Pipeline - Error Handler`. Save successful executions: switch to **Do not save** once testing is done (failed: Save).
- `Run Report Engine (14)` **deleted** (it referenced the unpublished Back-Half and blocked publishing); `CIS Engine - Report Back-Half` stays unpublished. The rest of the old chain is still disconnected for rollback.
- **Extraction prompt rewritten** (`Extract with Claude (6)`): names the form as the agency's Client Information Sheet, says it has no signature/SOA/appointment/coverage sections, marks injectables (Ozempic, Trulicity, Mounjaro, pens) `is_injectable`, and limits `flags` to medication concerns + unreadable name/DOB/ZIP. JSON field names unchanged.

**Engine (Railway)** — commits `b16ea57`, `a6e82dc`, `2e7923d`, `98805bc` (43 offline tests):
- **Concurrency:** Railway's Custom Start Command (`bash -c "python startup.py && gunicorn app.main:app"`) overrides the Procfile and ran gunicorn's defaults (1 worker, 30 s timeout). Added Railway variable **`GUNICORN_CMD_ARGS = --workers 3 --timeout 180 --graceful-timeout 30`**. Verified: 3 reports at once in 4.3 s vs 3.9 s for one (`_scratch\live_check_parallel.py`). No shared state between requests (checked: no module-level mutable state, PDFs built in memory).
- **Picker detail** on `/plans-for-zip` (premium, deductible, plan number, official name) from the same helper as the reports.
- **New internal report** (`app/internal_pdf.py`, landscape US letter): **one table, one column per plan** (pick order; Part D last with a **deep-teal header** and "PART D · DRUG PLAN" label). Rows: cost for the plan year (with "excludes <drug>" when an injectable/unidentified drug isn't priced), one row per medication (tier; colour only for Tier 4–5, not covered, not identified, injectables), pharmacy (cheapest nearby, mail order, first months), and **one row per doctor** (status for that column's carrier). Text auto-shrinks to keep page 1; overflow continues on page 2 with headers repeated. Old layout kept as `build_pdf_v1`: set Railway variable **`INTERNAL_REPORT_V1=1`** to switch back.
- **Doctor honesty:** rows show the name the client wrote; a directory match with a different first name (last-name-only false match, e.g. Sarah → Steven Johnson, DDS) shows amber **"Possible match … verify"**, never "In network"; "Not found — verify with carrier", never "out of network".
- **Injectables safety net** `is_known_injectable()`: GLP-1s/biologics and pen/injection wording are always "Verify coverage" even if normalization misses them; insulins excluded (they keep the $35 cap pricing).
- Live check script for the report: `python _scratch\live_internal_report.py` (fake client, opens the PDF).

**Verified with fake sheets today:** batch upload of 3; a Details-tab edit carries to both PDFs; Linda's amlodipine 20 mg flagged; Ozempic → "Verify coverage" and excluded from totals; both reports' yearly costs match. New fake test sheets on the agency's real form: **Ruth Anderson** (55125 Washington), **Gerald Okafor** (55337 Dakota, Ozempic pen), **Linda Marchetti** (55369 Hennepin, Synthroid brand, amlodipine 20 mg, no dentist).

#### Oct 2, 2026 (evening) — Messy-sheet test, flagged-field pipeline fixed, review UX queued
**Why:** Ruth / Gerald / Linda were typed and easy to read, so nothing ever reached "needs review". Claude made a deliberately messy fake sheet — **Harold Lindgren** (`Harold_Lindgren_CIS_TEST_NEEDS_REVIEW.pdf`, built by `make_messy.py`: handwriting font on the real form, tilted/shadowed/blurred phone-photo look, coffee ring). Planted problems: DOB `4/1?/1951`, 4-digit ZIP `5544`, cut-off phone + email, "Medica ?? / not sure", faded "Dr. Sch...dt", misspelled "Lisinpril", Metoprolol with no strength, "Atorvastatin 4O mg", **Januvia crossed out**, "Jardiance 10 or 25?", faded "Eliqu..s" with both Tablet and Capsule ticked, Furosemide "as needed".

**What the test showed:**
- ✅ Tab defaults confirmed in code: a sheet with nothing flagged opens on **Plans**; any flagged field → **Details**; a finished sheet → Details read-only. *(Gap noted: the app goes by `flagged_fields` only, not the overall confidence score.)*
- ❌ The reader **guessed** values instead of leaving gaps: ZIP 5544 → 55434, Jardiance "10 or 25?" → 10mg.
- ❌ Problems the reader noticed (misspelling, missing strength, crossed-out Januvia) went into free-text `flags`, **not** `low_confidence_fields`, so they were never orange. Januvia went onto both reports and was priced. Root cause: `Build Review JSON (19)` built `flagged_fields` only from `low_confidence_fields`, and the prompt defined that as "handwriting unclear" only.
- ❌ Internal report said **"In network"** for the illegible "Dr. Sch...dt" (matched on clinic) — a dangerous false positive.
- ❌ Details tab is one long flat list of every extraction leaf, including reader internals (Confidence, Low confidence field 1–5, Flag 1–6 as editable boxes); tall Flag boxes clipped their first line; no way to tell must-fix from optional; no way to remove a crossed-out drug.
- ❌ Flagged (orange) fields auto-saved silently: no Save, Enter did nothing, no confirmation. Edit link was plain black text.

**Engine (Railway) — commits `a944eeb`, `21dd8d1` (47 offline tests):**
- `a944eeb` Internal report **section bands** (COST / MEDICATIONS / PHARMACY / DOCTORS) bolder: darker band `#DCE3EC`, full-size near-black title, dark rule above.
- `21dd8d1`:
  - Reader notes on the report are **readable**: field paths stripped (`drugs[1].dosage:` → gone, `zip_code` → "ZIP", `providers[0].last_name` → "Doctor's name"); the generic "couldn't identify" note is dropped when a more specific note (e.g. "may be a misspelling") covers the same drug.
  - **Illegible doctor name** (contains `...`, `…` or `?`) is never plain "In network" → amber **"Possible match — verify"**.
  - Drug with **no strength** gets an amber line under its name: "no strength on sheet — cost uses a common strength".
  - Contract-plan number removed from inside official plan names (Medica "Advantage Solution H6154-001 (HMO-POS)" showed the number twice) — in `display_plan_name`, so both reports benefit.

**n8n main workflow (to publish + test):**
- `Extract with Claude (6)` prompt v3: **NEVER GUESS OR COMPLETE VALUES** (DOB, ZIP, phone and every dose exactly as written, `?` for an unreadable digit); new per-drug **`crossed_out`** (true for struck-through / stopped drugs, still listed); new **`field_notes: [{field, note}]`** — one entry per field an agent should look at, using exact paths, covering unclear handwriting, crossed-out drug, missing dose, ambiguous/unusual dose, possible misspelling, incomplete DOB/ZIP/phone; `flags` kept (plain sentences starting with the drug/field name — the internal report still uses them); `low_confidence_fields` = every path in field_notes.
- `Build Review JSON (19)` v3: `flagged_fields` = field_notes paths ∪ low_confidence_fields ∪ paths found inside flags ∪ **deterministic checks** (drug with no name; name but no dose; dose containing `?` or "or"; `crossed_out` → flag `.name` and add a note if the reader didn't; ZIP not 5 digits; DOB not MM/DD/YYYY). Cleans `field_notes` and writes it back into the extraction for the app.
- `Parse JSON (7)` unchanged (passes new fields through).
- **Test pending:** re-run Harold → in the n8n execution, Parse JSON (7) output should show ZIP `5544`, Jardiance dose `10 or 25?`, Januvia `crossed_out: true`.

**Roundabout (Claude Code) — queued, in order:**
- **22** Pin "Your selection" above the plan list (compact rows, own scroll after ~4); medications grid columns hug content (no mid-card gap); client-card facts line styled like the section headings (bold small-caps labels, regular values).
- **23** Edit / Save change links in **blue** (`accent_text`, lighter blue in dark mode); flagged fields get a blue **Save** link + Enter saves → green "✓ Saved" state; autosave stays as crash safety but doesn't confirm; **Submit locked until every flagged field is saved** (even if unchanged); confirmed state persisted in the draft (`_confirmed`, never sent as a correction).
- **24** Plans tab room on small windows: remove the "On the reports" summary box + Edit details button (keep one amber line for e.g. "Metoprolol has no dosage — fix on Details"); "Client sheet" tags on selected rows instead of the paragraph; selected plans marked in the list (number + tint); selection capped at ~⅓ height, collapses to "5 selected ▾"; plan list always ≥ 3 rows; draggable scan/panel divider (remembered).
- **25** Details tab redesign: hide reader internals (confidence, signature_present, low_confidence_fields, flags, field_notes; SOA/appointment/coverage/custom_plans in a collapsed "More from the sheet"); **cards** (Client info, one per drug "Drug N · name", one per doctor); **two levels decided by the app from the field path** — MUST FIX (orange, blocks Submit: ZIP, DOB, drug name, drug dosage, crossed-out drug) vs CHECK (amber outline, optional: phone, email, doctors, names); reader notes shown under their field (field_notes first, flags text-match fallback, unmatched → "Other notes from the reader"); "2 must fix · 3 to check" line; **Remove drug / Keep drug / Undo** (`_removed: ["drugs[3]"]` in the draft; value corrections applied first, removals in descending index). **Addendum:** `drugs[i].crossed_out === true` is the primary crossed-out signal; never shown as a field; field_notes hidden.

**Decisions / rules from this session:**
- **The app decides severity, not the reader.** Must-fix = anything that changes price or plan list. Predictable, testable, doesn't drift with model wording.
- **Honest gap beats confident guess** for DOB, ZIP and doses — the agent fixes it by hand.
- A crossed-out drug stays on the list but blocks Submit until the agent keeps or removes it.
- Plan-picker and Details changes are layout-only; selection/ordering/saving logic untouched.

#### Remaining before Saturday (in order)
*Evening update (Oct 2):*
- ✅ Phase 21 done (test-data reset + Help "contact Jordon"). Ruth / Gerald / Linda re-run through the new build — uploads, status changes and tab defaults all good.
- ⬜ Push engine commits `a944eeb` + `21dd8d1` (`git push origin main`) → Railway redeploys.
- ⬜ Publish n8n prompt v3 + Build Review JSON v3 → re-run Harold → check Parse JSON (7) output (ZIP 5544, "10 or 25?", Januvia crossed_out true).
- ⏳ Review Claude Code reports for Phases 22, 23, 24, 25 (+ addendum), one at a time; hands-on check each with Harold + one clean sheet.
- ⬜ Then the release build + Lacey items below.
*Updated end of Oct 2 — the original list below it is done (✅ items 1–3; item 4 continues here):*
- ⏳ **Phase 21** test-data reset + Help contact line (running).
- ⬜ Re-run Ruth / Gerald / Linda for a clean demo set; check Gerald's report (Ozempic + new prompt, no Form-notes noise).
- ⬜ **Release build:** one full test run + final exe (the one Lacey gets).
- ⬜ **Lacey's n8n copy** (on the call): duplicate ONLY `SOA Automation Workflow`; switch the credential on the 7 live OneDrive nodes (`List Incoming (2)`, `Download PDF (5)`, `Write Review File (20)`, `Download CIS (Low) (21)`, `Copy CIS to Needs Review (22)`, `Delete from Incoming (Low) (23)`, `Delete Meta Sidecar (25)`) **plus the new claim nodes `Claim File (30)` and `Delete Claim Marker (33)`**; delete the old disconnected chain in her copy; set the Error Workflow. Her Microsoft sign-in may need admin consent (have the Twin Cities admin login ready). **Unpublish her copy after the test** (Railway isn't approved for real client data).
- ⬜ Her PC: Smart App Control status; OneDrive account type (personal vs work).
- ⬜ Feedback questions for Lacey: what was confusing, does the picker show what she needs, would she hand the client sheet over as is, what's missing on the internal report; show her the layout mockup.

*Original list (Oct 2 morning):*
1. **Review Claude Code's Roundabout Phase 12 report** (picker, both reports generated in-app, every sheet paused for review). Check: tests written failing-first, reconcile/status handling, file naming + sidecar, idempotency (no double reports), and that the app sends `plan_year` to `/process-soa` (without it the internal report falls back to the Oct–Dec cost period).
2. **n8n change** (one node at a time): main workflow `Route by Confidence (8)` true output → `Build Review JSON (19)`; leave the old high-path nodes disconnected for rollback; unpublish `CIS Resume - After Review`. Publish with a version name + description.
3. **End-to-end test** with a fake sheet: upload → review → pick → both PDFs.
4. **Lacey prep:** fake test-sheet pack; exe via OneDrive with a versioned filename (freeze the exe for AEP); dry run on a clean machine; check whether **Smart App Control** is on her PC (it blocks unsigned apps with no bypass, unlike SmartScreen's one-time "Run anyway"); confirm her OneDrive account type and sign-in.

#### Closed from the Sept 23 backlog (by this change)
- ✅ Internal report Section 2 "Not identified" (Oct 2).
- ✅ Quartz (H9834) carrier name: the agent-chosen path names carriers by contract number (`CONTRACT_CARRIER`), which includes Quartz.
- ✅ "May a client sheet show two plans from the same carrier?" Moot: the agent chooses.
- Retiring with the resume workflow: resume `Log to Sheets (10)` retry, "centralize naming + sidecar in the shared engine" (#8, the naming now moves to the app), main/resume drift.

#### Oct 1, 2026 — 2027 data plan: formularies + providers (meeting with Jill)
**Status (Oct 2):** CMS **2027 Landscape PUF is in hand** (all 2027 plans, premiums, deductibles). The CMS 2027 **formulary** files aren't out yet (Jordon expects ~mid-October, close to AEP's Oct 15 start). Carriers have **already published their 2027 formularies as PDFs** on their websites (Jill has access; we may be able to find them ourselves).

**Formulary plan (Jordon + Jill):** collect the carriers' 2027 formulary PDFs and build our own 2027 formulary database from them, so the engine has 2027 drug coverage before CMS publishes.
- **Gap to solve first:** the carrier PDFs give each drug's **tier** and restrictions (prior authorization, step therapy, quantity limits). The engine's dollar estimates also need (a) each plan's **cost per tier** (copay/coinsurance by tier and pharmacy type) and (b) **drug prices**. Today both come from CMS files, not formulary PDFs. Options: tier costs from each plan's 2027 Summary of Benefits / Evidence of Coverage (also carrier PDFs); drug prices from the 2026 CMS pricing file as a stand-in, labeled as an estimate.
- **Name matching:** the PDFs list drug names, not the RxNorm IDs the engine matches on. Each entry must be mapped to RxNorm IDs, and errors here mean wrong "covered / not covered" answers. Needs spot-check validation against carrier lookups.
- **Treat it as a bridge:** when the CMS 2027 files land, load them, **compare against our PDF-built database** (to measure how accurate the bridge was), then switch to CMS as the source of truth.
- **Open decision:** whether the build effort (parsing ~8 carriers' PDFs + tier costs) pays off for the gap between now and the CMS release. If CMS lands within days of Oct 15, a smaller bridge may be enough: agents verify on Medicare Plan Finder, or we cover only the most common drugs.
- Also needed for 2027: PBP benefits + Star ratings (client sheet benefits), the `DATA_VINTAGE` string, and the internal report's hard-coded footer.

**Provider plan (Jordon + Jill):** no reliable public source exists, so build our **own provider network database**, starting with a cheat sheet of what Jill, Lacey and the other agents already know (high level, e.g. "Fairview clinics → in network with plan X"). During AEP, agents send in what they confirm (e.g. "Dr. Johnson, Blue Cross → in network"), and Jordon keeps adding to it, so it grows and agents have less to check as the season goes on.
- **Design points to settle before building:**
  - Networks can differ by **plan**, not just carrier (one carrier can run several networks). Record the plan/network, not only the carrier.
  - A clinic being in network doesn't guarantee every doctor there is. Keep clinic-level and doctor-level entries separate and label which one matched.
  - Every entry carries **who confirmed it and when** (and for which plan year). Networks change year to year and mid-year.
  - Identify doctors by name **+ clinic + city** (or NPI when known), not last name alone. The current page-2 lookup's last-name-only matching already returns wrong people.
  - **Absence ≠ out of network.** The report should only ever say "in network (agent-confirmed <date>)" or "verify," never "not in network" just because we have no entry.
  - Submissions must contain **no client names**: only the provider, plan and status. That keeps the database free of client health info.
  - Collection: a simple structured form (or a button in Roundabout) beats loose notes Jordon retypes. Less work for him, fewer typos.

#### Oct 2, 2026 — Proposed: app-only setup (no n8n or OneDrive per agent) — replaces per-agent cloning
*Jordon's idea, agreed in principle. Not before Saturday's test. Scope it after Saturday as the replacement for the per-agent n8n cloning step.*
- **Idea:** n8n's only remaining job is reading the sheet. Move that into the engine, and the app works on a normal local folder. Each agent just installs the app: no per-agent n8n workflows, no OneDrive sign-ins through Jordon's n8n, no Microsoft admin-consent dependency. Every agent uses a secured computer.
- **New flow:** app → engine "extract" step (Claude via Bedrock, on the BAA host) → agent confirms + picks plans → app → engine reports → PDFs saved in the local folder.
- **Design requirements:**
  - **Reading happens on the server, never in the app.** An API key inside the exe can be dug out. The key stays on the engine (Bedrock), so this depends on the host migration off Railway (AWS or Azure, BAA in place).
  - **Backup + record keeping.** Local-only files are lost with the laptop, and Medicare record-retention rules apply (e.g. SOA records kept for years). The app saves to a plain folder the agent can place inside their own OneDrive (synced by Windows, not by n8n), which gives backup and second-computer access with zero credentials on Jordon's side. Secured PCs should have drive encryption (BitLocker).
  - **Engine access keys.** Once apps call the engine directly with client data, each agent/app gets its own key; today the engine is open to anyone with the URL. Add with the host migration.
- **Open questions:** how agents get phone scans (CamScanner) onto the PC (watched folder via their own OneDrive sync, or drag into the app; ask Jill); the cutover for sheets already in OneDrive folders; whether the n8n Error Handler alerting gets an app/engine equivalent.
- **Sequencing:** host migration + Bedrock first (needed regardless) → engine extract endpoint → app local-folder mode → retire n8n for this pipeline. This is "Vision 1" of roadmap #17 (consolidate own setup), pulled forward.

#### Oct 2, 2026 — Roundabout layout redesign (agreed, build after Saturday)
> ✅ **Built the same day** (Phases 16–20), except "Run again with different plans" (deferred) and Plans (placeholder "Coming soon"). Upload page rules agreed: the list shows only uploading/reading sheets; finished rows show a green check ~10 s then clear; unreadable sheets stay red until dismissed; the section hides when empty; big batches show 5 + "and N more". Completed Client Reports refinements: one entry per client (grouped by name + DOB), new reports bump the client to the top, "Latest"/"New" tags on report cards. The internal report redesign mockup lives on the same canvas.

Jordon approved a mockup (Design canvas "Roundabout — New Layout Mockup"). Sidebar becomes:
- **Upload** (top): drop zone for Client Information Sheets (PDF or phone photo, several at once).
- **To Do**: ONE list replacing Choose Plans + In Progress + Failed. Each row has a status tag ("Reading the sheet…", "Ready — choose plans", "N fields to check", "Generating reports…", red "Couldn't finish" with reason + Retry/Delete) and one action button.
- **Completed Client Reports** (Jordon's name): replaces Completed + Client Sheets + All Files. Client list (names only, no extra counts), client detail with every report run (date, plans picked, internal report + client sheet + original scan), plus **"Run again with different plans"** (reuses stored sheet data, no rescan) and **"Compare all plans"**.
- **Plans (later)**: the in-app Medicare.gov substitute. Always driven by a client's uploaded sheet, never manual typing: every plan in the ZIP with premium, drug deductible, estimated yearly cost for the client's drugs, stars, coverage; pick to compare side by side or create reports. Agent research tool, so sorting by cost is fine here; the client sheet still only shows the agent's picks. Needs 2027 data.
- **Order:** Saturday test on current tabs → To Do + Upload merge (before Oct 15 if time) → Completed Client Reports (pairs with the app-only/local-folder move) → Plans (after AEP / after 2027 data).
- **Open question for Jill:** "look up other plans" = for that client ("Run again") or general browsing (Plans)?

#### New notes + backlog (Oct 2)
- **After Saturday (app):** "Run again with different plans"; move the routine folder check off the UI thread; put Roundabout under git (and fix the update-check repo); the Plans page (after 2027 data).
- **n8n cleanup** (after Lacey's test + a few days of real use, export first): delete the old disconnected chain and `Route by Confidence (8)`, delete `CIS Resume - After Review` and `CIS Engine - Report Back-Half`, rename `Every Minute (1)`, set "Do not save" successful executions.
- **Provider matching** still matches on last name only; the report now flags conflicts as "Possible match", but the real fix is the agent-built provider database (Oct 1 plan).
- **Audit log:** the SOA Tracker Google Sheet stops getting rows when the n8n report paths stop. Decide: app-side log, or retire it.
- **Data:** still CMS **Q1 2026** (`DATA_VINTAGE` in `main.py`; the internal footer string is still hard-coded too). The **2027 data refresh (#13) is still required before real AEP use.**
- Hennepin has **no Humana Medicare Advantage** plan in the local data (only Humana Part D). Confirm against 2027 data.
- ZIP 56601 (Bemidji) maps to Hubbard County in `zip_county`. Worth a spot-check during the refresh.
- Part D star ratings aren't loaded (client sheet shows "—").
- **SmartScreen / Smart App Control:** to avoid blocked installs during AEP, consider **Azure Artifact Signing** ($9.99/mo; US individuals are eligible; needs a paid Azure subscription + identity validation).
- **Bedrock swap paused:** Option A approved (upgrade n8n 2.19.5 → 2.41.3, which fixes the SigV4 `bedrock-runtime` bug from 2.26.0; then an HTTP Request node + AWS credential, model `us.anthropic.claude-sonnet-4-6`). Waiting on the read-only Docker check before the upgrade. The engine's own `normalize_drugs` Claude call also needs to move to Bedrock.

### ═══ SEPTEMBER 2026 UPDATE ═══
*Last updated: Sept 23, 2026. The August plan below is preserved as history; this section is the live picture.*

#### Sept 23, 2026 — Client sheet live + polished, resume path proven, internal banner cleaned (this session)
**Client-facing sheet (Phase 3 / #14):** live end-to-end through the real pipeline on **both** paths: front (high-confidence) and resume (corrected). The resume-path emit (nodes 22/23) was proven with a deliberately unreadable 42%-confidence test CIS ("Walter Pemberton"). Polish shipped: the real TCHIS logo (it had never actually shipped; see the correction in the Phase 3 doc), half-star ratings, comma formatting, official CMS plan names with plan type (e.g. "UHC MN-0001 (PPO)"), UnitedHealthcare branding, carrier-headline / plan-subtitle header, auto-fit columns, and a one-page guarantee. **Honesty fix:** the sheet never says "not covered" for a drug the engine couldn't identify or skipped (injectables); it says "to confirm with your agent" and the cost row says "excludes N medications." Full detail in the Phase 3 doc.
**Internal report (Goal B follow-up):** the warnings banner is now compact and grouped (**Medications to verify / Form notes / Plan requests**), one row per group: 18 rows became ~3 lines on the Walter test. SOA-era extraction flags (signature, SOA date, appointment date, coverage types) are dropped; the header reads "DOB: — · Zip" (no SOA Date). Applied via verify-or-abort `patch_warnings_banner.py`; proven with `test_internal_report_warnings.py`.
**Roundabout Phase 11** (client sheet in the Completed + All Files three-dot menus; CLIENT SHEET column on All Files, paired to the same completed sheet as LAST REPORT): built by Claude Code, 799 tests green, exe rebuilt. **Manual checklist deferred** (`M:\Roundabout\docs\checklists\phase11_manual_checklist.md`; note step 14 should expect the compact amber "Retry" on All Files, not the long label).
**Resume workflow hardening:** `Build Extraction File (22)` set to On Error: Continue ("Resume v1.1"), so a client-sheet emit failure can never halt archive/cleanup. *Confirm it was published.*

#### Locked go-live order (Sept 23, Jordon)
1. **Core architecture** (now): app items, client-sheet refinements.
2. **Compliance:** n8n → **Amazon Bedrock** swap first (AWS BAA signed and Claude Sonnet 4.6 set up on Bedrock, Sept 23), then the **host migration** off Railway (Phase 3 doc Part 10).
3. **Folder restructure** (below) + the test-data reset (#4).
4. **Final end-to-end tests** of both paths on the final layout.
5. **Per-agent cloning**, which must come **after** step 3; otherwise every folder change is multiplied by four.

**Compliance map to verify (Bedrock alone isn't "compliant"):** the n8n Claude extraction (direct Anthropic today; Bedrock fixes it); Railway (host migration fixes it); the **Cloudflare Worker prompt-caching proxy** (confirm whether the CIS extraction routes through it; if so, Cloudflare sees client data in transit); **OneDrive** (docs reference a *personal* account in places; consumer OneDrive can't sign a BAA, Microsoft 365 Business can; confirm what the live workflows point at); the n8n home server (basic safeguards). The Bedrock swap choice (native AWS Bedrock node vs HTTP Request) affects whether the Cloudflare Worker caching setup still applies; decide that first.

#### Folder restructure — designed, DEFERRED to step 3 (supersedes #10)
Target layout, so agents opening the folder see only finished files:
```
Client Info Workflow/
├── Completed Reports/           ← internal drug-cost reports (was "Reports"), PDFs only
├── Client Comparison Sheets/    ← client-facing sheets (already exists)
├── Client Information Sheets/   ← original CIS scans (was "Archive")
└── System/                      ← Incoming, Needs Review, Completed Review, Failed + ALL JSON sidecars
```
- **Why System/:** `<ULID>__extraction.json` holds the client's name + medication list (PHI) and currently sits loose in `Reports/`; `reportmeta` is only hidden by a local Windows attribute (still visible on OneDrive web/phone). Rejected: app hides extraction.json too (band-aid, local-only); app deletes it (breaks Retry, violates file ownership).
- **Build plan:** app first (a Roundabout phase: new layout; read sidecars from `System/` AND old `Reports/` during transition; Delete + client-sheet generation updated; hide `System/`) → n8n: add a **Config node with all folder paths** to main + resume (Engineering Principle 9; one edit per future rename), repoint every path node (~15–20) → test-data reset → full end-to-end test.
- **Open questions for then:** (a) do agents ever save CamScanner scans straight into OneDrive? (decides whether an upload folder stays visible outside `System/`); (b) rename the internal report file so it isn't confused with the client sheet ("Plan Comparison" vs "Comparison Sheet"), e.g. "Drug Cost Report 2027".

#### New backlog (Sept 23)
- **Internal report Section 2:** show "Not identified" instead of "Not Covered" for drugs the engine couldn't identify. The banner already says so; the tier grid contradicts it. Small `main.py` patch.
- **#9 extraction-prompt cleanup** now also fixes banner noise at the source: the prompt's flags instruction still asks for signature / SOA date / appointment date / coverage types. Consider categorized flags (`medication` / `form`).
- **Injectable tagging:** the engine priced Ozempic as covered (normalization didn't mark it `is_injectable`). Check how `is_injectable` is set vs the agency's injectables rule.
- **Quartz (H9834)** is missing from the client-sheet carrier families (`client_comparison._CARRIER_FAMILIES`), so its carrier line shows a raw label.
- **Question for Jill:** may a client sheet show two plans from the same carrier? (The Walter test picked two Aetna plans; selection ranks purely by fit + cost.)
- **Resume `Log to Sheets (10)`** has no retry and stops on error; a Google Sheets hiccup halts archive + cleanup. Add retry or continue.
- **Repo hygiene:** the repo copy of this doc and many build/patch/test scripts are untracked in git. Decide tracked vs `.gitignore`.

#### Sept 19, 2026 - Engine health check + agent-report visual refresh (this session)
**Goal A (audit / health check) - DONE, all green.** Engine up (commit `084460e`, June 2 code); all 7 R2 databases validated with correct counts; `/health` clean (65 plans, 888 zip-county, 1,587 service-area, 55309 -> Sherburne). Core data is CMS **Q1 2026** (expected pre-AEP). **Cost engine verified correct** (free Tier-1 generics -> $0; a brand-drug control -> real deductible-ramp costs + per-pharmacy fees). **ZIP-only pharmacy fallback confirmed working** - it degrades to the ZIP centroid, it does NOT break (this resolves the "pharmacy breaks on ZIP-only" worry below). Details in the "September 19, 2026 Session Detail" under Build History.
**Flags-banner - root cause found.** Claude's `flags` array is never sent to the engine (not in the n8n **Build Railway Body** node, not read by `/process-soa`); the warning banner only renders engine-internal warnings. Fixing it needs BOTH n8n (add `flags` to the payload) and the engine (read them, merge into `build_pdf` warnings). This supersedes the "do flags render?" check in step 2 below - the answer is "they can't yet, because of this specific gap." **✅ RESOLVED (Sept 21, 2026)** — both halves were built and proven: the engine now reads `flags` and merges them into the `build_pdf` warnings, and the n8n **Build Railway Body** node now sends the `flags` array. Verified end-to-end on a real flagged sheet (banner rendered). See the September 21, 2026 Session Detail below — Goal B is now fully closed.
**Goal B pt.1 - agent report visual refresh, DONE & live.** Three verify-or-abort patch scripts on `build_pdf`: (1) slate blue-gray palette + removed the best-plan star/column highlight (Sections 1 & 4); (2) two-up (2x2) pharmacy layout + section spacing; (3) removed the leftover best-plan highlight Sections 2 & 3 build inline. Every plan now shown equal; still one landscape page; sizes kept. **Remaining piece of Goal B = the flags -> PDF plumbing above — ✅ now DONE (Sept 21, 2026); Goal B is fully closed. See the September 21, 2026 Session Detail below.**
**Repo hygiene.** `.gitignore` was UTF-16 (git couldn't read it, so `.env` with the R2/Anthropic keys was exposed to an accidental `git add .`); rewrote as UTF-8. Quarantined three dead `main.py` copies to `_archive/`. Confirmed entrypoint `app/main.py` (Procfile) and local == deployed.
**Footer caveat (new):** the report footer hard-codes "CMS Medicare Formulary Q1 2026" as static text - it does NOT read the actual data vintage, so it must be updated in lockstep with any data refresh (else the report lies about its own data). Fold into #13 / #11.

#### Where we are now
The engine is well past "production-built but idle." Since August we've built the **Phase 8 human-in-the-loop review loop** and shipped **Human-Readable Filenames** (client-named reports/archives matched via a hidden ULID sidecar — this was the feature I'd confusingly labeled "Option A"). Both are documented above in the Current Architecture section.

**Done & verified:**
- ✅ Phase 8 review loop — a low-confidence sheet routes to Needs Review, gets corrected in the Roundabout app, and the resume workflow finishes it. Proven end-to-end.
- ✅ **Human-Readable Filenames** on both paths — reports and archived originals are human-named; the app matches via a hidden `<ULID>__reportmeta.json` sidecar; the client's name shows even for high-confidence sheets.
- ✅ Naming-collision + null-crash + orphaned-meta bugs fixed (details above in Current Architecture).
- ✅ App speed: 10s reconcile + active placeholder hydration (kills the manual-refresh feeling) + an in-progress spinner. Confirmed by Jordon — felt time dropped from "3–4 min" to "under a minute."

**In progress:**
- ✅ **App: duplicate-run numbering (Sept 18).** Completed / All-Files rows now show a subtle `(2)`/`(3)` after the client name for repeat runs, derived from the *report filename* (not a row count) so it always matches the OneDrive folder even after deletions. With this, the app is confirmed in a good, "good enough for AEP" state — nothing app-side is blocking go-live.

#### Near-term (finish Human-Readable Filenames + hygiene) — do these next
1. ✅ **Folder-based naming confirmed on BOTH paths — DONE (Sept 18).** Reads the actual `Reports/`/`Archive/` folders and picks the lowest free number, so a deleted file frees its number and nothing collides. Verified: Margaret Klein run 3× produced `(2)`/`(3)` in OneDrive *and* in the app, no overwrite. (Details above → "File naming / de-duplication".)
2. ✅ **Resume twin — DONE (Sept 18).** Added `List Reports Files (20)` + `List Archive Files (21)` and rewrote `Parse & Prep (7)` to use the same folder-based `nextName()` logic as main (report + archive both), with the corrected-file read switched to an explicit `Get Corrected (6)` reference so the inserted nodes couldn't break it. Both paths are now true twins, tested end-to-end. **Duplicate behavior (the parallel item) is settled too:** both paths produce real numbered duplicates (no overwrite), and deleting a file frees its number — folder and app agree.
3. ✅ **Remove dead nodes — DONE (Sept 10).** `Count Existing Runs (9)` + `Aggregate Runs (10)` deleted; they were unused once folder-based naming replaced the row-count dedup.
4. **Full test-data reset** before go-live — clear the SOA Tracker sheet's data rows, wipe leftover test files from all OneDrive folders (Reports, Archive, Needs Review, Completed Review, Failed, Incoming), and delete the test sheets in the app so it matches. (This is also what makes the file numbering start clean.)
5. ✅ **Reconcile the master doc — DONE (Sept 10).** Phase 8 + Human-Readable Filenames were folded into the Current Architecture section, both the vault + repo copies updated byte-identical, and the two standalone build docs retired.

#### Mid-term (reliability + polish)
6. **Error-file coverage** — the **main** workflow has no node that writes `Failed/<ULID>__error.json`, so a hard mid-run failure emails Jordon but shows the sheet stuck "In Progress" (limbo) instead of on the Failed tab. Add a main-side error-file writer, and wire the resume `Write Report Sidecar (19)` error output into `Build Error File (17)` (minor — it has retry, but for consistency).
7. **Poll speed / concurrency (pipeline side)** — dropping the n8n poll below ~60s risks two overlapping polls grabbing the same Incoming file (the file sits in Incoming for the whole run, and the main workflow has no "claim on pickup" guard). If we want faster pickup, pair it with a claim step (move to a Processing marker on pickup). Note: the *app* side is already event-driven via the watcher; this is only the n8n intake.
8. **Centralize naming + sidecar in the shared engine** — report-naming + sidecar logic is currently duplicated across the main and resume workflows, which is exactly why they keep drifting (bugs fixed in one but not the other). Move it into the shared `CIS Engine` sub-workflow so both paths get identical behavior for free. This is the proper structural fix.
9. **Clean SOA-era fields from the extraction prompt** — `soa_date` and `appointment_date` don't exist on the Client Information Sheet, so Claude returns null and it causes downstream trouble (it already caused the Count Existing Runs crash). Remove them from the prompt + the couple of columns/nodes that reference them.
10. **Folder renames** — Archive → "Completed Client Sheets", Reports → "Plan Comparisons". Touches app + n8n (both address folders by path), so do it deliberately as its own step.
11. **`planYear` auto-flip** (low priority) — replace the hardcoded `2027` with a rule that flips to next year on Oct 1 (aligned with AEP + CMS data release), with a manual override. Keep it tied to whatever plan-year data is actually loaded so the filename never lies about the data.
12. *(Optional)* App-Delete could also wipe the Archive copy if we ever want report + archive to behave identically on delete (currently Delete leaves Archive alone by design — it's the permanent record).
- **Pharmacy lookup breaks on ZIP-only intake (data engine).** The Client Information Sheet collects only **ZIP + county**, not a street address. That's enough for *plan / formulary* lookups (CMS only needs the area), but the drug engine's **pharmacy** lookup needs a finer location than ZIP+county to find nearby pharmacies and pull pharmacy-specific pricing — so it currently breaks. **Decision (Jordon, Sept 10): do NOT add address to the form.** Instead, make the engine degrade to **ZIP-level pharmacy pricing** so a missing address doesn't break the run — it just makes the pharmacy piece less precise. This lives in the drug engine (`main.py` / the pharmacy-lookup logic), not in n8n. *(Sept 19: confirmed the fallback already works — it degrades to the ZIP centroid, it does not break.)*
- **Engine + report-PDF work (data engine / main.py).** Jordon's active next-work: (a) further work on the engine that pulls the plan/drug data, and (b) **edit the PDF the engine produces** — layout/content of the internal drug-run report. Groups with the pharmacy ZIP-fallback (above) and the still-open "do flags render as a PDF banner?" check — all live in the Railway app (`main.py` / ReportLab). Scope the specific PDF edits when we start. *(Sept 19 update: the PDF **visual** refresh is DONE — slate palette, best-plan highlight removed, two-up pharmacy grid, section spacing, all live. The pharmacy ZIP-fallback is confirmed already working. ✅ Sept 21: the flags-banner is now DONE — n8n sends `flags`, the engine renders them as the ⚠ Drug Verification banner, verified on a real flagged sheet; Goal B fully closed. See the September 21, 2026 Session Detail below.)*

#### AEP rollout — multi-agent (the four family agents)
> **AEP-critical.** The whole point is for **Jenna, Lacey, Jacob, and Jill** (all inside Twin Cities Health) to run this from their own machines / OneDrive accounts during AEP — they can't until this is built. This + the data refresh (#13) are the two true AEP go-live blockers, so this comes *before* the next-feature work (client-facing / image intake).

**REVISED approach (Sept 18) — copy the OneDrive-touching workflows per agent.** The "one workflow, four triggers" idea was reconsidered and dropped. The blocker is a hard n8n constraint: **a node's credential is set on the node itself and can't be swapped per-run from the incoming data.** You can make a *URL* dynamic with an expression; you can't cleanly make "which OneDrive credential this node uses" dynamic. So one shared chain of HTTP nodes can't fluidly be "Lacey's account" on one run and "Jenna's" on the next — which pushes toward per-agent copies of the OneDrive-touching workflows.

**What gets copied vs. shared:**
- **Copied per agent: `main` + `resume`** — the two workflows whose HTTP nodes touch OneDrive. Each agent's copy has *their* credential set on those nodes (set once per copy). **Folder paths stay identical** across agents — each agent has their own `Client Info Workflow/Reports|Archive|…` structure in their own OneDrive, so only the *credential* differs, not the paths.
- **Shared (NOT copied): the `CIS Engine` + the `Error Handler`.** The engine has zero OneDrive credentials (it just calls Railway + returns a PDF); the error handler is a global Error Workflow. All agents share the one of each.

**So the count is ~10 workflows, not 16:** `main ×4` + `resume ×4` + `1 shared engine` + `1 shared error handler`. Eight are near-identical clones set up once.

**Could main + resume merge into one (multi-trigger) → ~6 total?** Technically yes (n8n allows multiple triggers per workflow), but **not for this AEP** — it makes an already drift-prone pair bigger and more tangled, then clones that tangle four times, right before the deadline. The *right* consolidation is #8 (centralize naming/sidecar in the shared engine so main/resume go *thin* and copies barely differ) — a post-AEP refactor, not deadline surgery.

**Concurrency — confirmed fine for four.** n8n queues executions and works through them; each item carries its own identity, so every report routes back to the right person. All four uploading at once = it still works, just a few seconds of queue wait. The real bottleneck is Railway (Claude + DB lookups + PDF), which is fine at this scale — it would only matter with dozens hammering it at once.

**⚠ Azure / Graph admin — keep it.** For each agent to authorize their OneDrive against the n8n app, the Twin Cities Microsoft tenant *may* require **admin consent** (depends on tenant config — some allow user self-consent, many require an admin to approve third-party app access). So do **not** give up Twin Cities Graph/Azure admin access until the multi-agent auth is verified working end-to-end for all four accounts — cheap to keep, expensive to get back mid-AEP if a consent prompt blocks a rollout.

**Still to produce:** a mechanical **per-agent cloning checklist** — exactly which nodes need the credential swapped, confirming paths stay identical — so each agent's setup is copy-paste, not error-prone.

**Longer-term scaling (post-AEP):** thin per-agent trigger workflows → shared engine, or a shared **OneDrive for Business** workspace (ties to #15 / HIPAA). Not needed for this AEP's four family members.

#### Big future elements (own projects)

> **Jordon's priority (Sept 10):** once the near-term pipeline items are squared away, the **next feature focus** is (a) the **client-facing plan comparison (#14)** and (b) **image-upload intake (#16)**. Not the highest priority — the near-term pipeline cleanup + test-data reset come first — but these two are the next real *feature* work, ahead of the mid-term reliability polish.
13. **Harden `quarterly_refresh.py` into a validated one-button CMS data refresh.** These aren't one clean file — formulary/Landscape (quarterly, URLs change each release), NPPES (multi-GB monthly), and per-carrier provider directories (Medica FHIR, BCBS/Humana/UHC PDFs, hardest part). Target = **automate the tedious 95%, human confirms the swap**: fetch → build fresh SQLite DBs into staging (never touch live) → **validate hard** (row counts + known-good spot-checks; refuse to publish on failure) → report what changed → wait for go-ahead → delete-then-upload to R2 + Railway redeploy. `quarterly_refresh.py` has never had a real end-to-end run — proving *one* clean refresh before go-live is the real AEP data dependency. Start by reading the current script to see how far each source already is.
    - **RESEARCH TASK (Sept 10, Jordon's idea):** CMS now exposes this data via a public **REST/JSON API on `data.cms.gov`** — the Quarterly Prescription Drug Plan Formulary, Pharmacy Network & Pricing PUFs, reportedly fully open with no API key. Investigate whether querying the API is a cleaner way to **automate the quarterly refresh** than downloading a ~2GB zip and rebuilding SQLite each quarter. Questions to answer when we do #13: (a) is it the *same* quarterly data? (very likely yes — so it wouldn't make data *fresher*, just easier to load); (b) rate limits / reliability for our heavy per-client lookups — a local SQLite DB answers in milliseconds, whereas hammering a public API during AEP could be slower or throttled, so the local-DB approach may still be the right call; (c) **also check for OTHER CMS datasets/APIs — plan information, benefits, provider/pharmacy data** — which could improve accuracy or change how we pull info, not just the formulary. Do the real research (read the current API docs, test endpoints, check limits) when we pick up #13 and give a proper “live API vs. local DB” recommendation. Likely outcome: use the API to *feed the refresh*, keep the local DB for *lookups* — but confirm.
14. **Client-facing Plan Comparison PDF** — a SECOND output on ~90% of the same engine. Reads the same Client Information Sheet, but instead of the exhaustive internal drug-run it **selects the 2–3 best-fit plans** and generates a branded Twin Cities Health Insurance Solutions comparison the agent hands to the client (reference mockup: `TCHIS_Plan_Comparison_mockup.pdf`). **The hard/new part is the best-fit selection logic** — it's a *recommendation*, which carries CMS marketing/steering compliance weight, so it must be rule-based, explainable, and consistent, not a black box. Two hard requirements: (a) internal vs client-facing outputs must **never blur** — an internal drug-run with provider names can't accidentally go out as a client handout, so design a clean generation switch in from the start; (b) accuracy stakes are higher (client's enrollment decision + the agency's license), so **this depends on the data refresh (#13) being solid — don't ship client-facing on stale data.** Right sequence: internal tool (done) → UI (in progress) → solid data refresh → then client-facing.
15. **OneDrive for Business migration (HIPAA BAA).** Needed for compliance anyway; it also unlocks reliable Graph webhook *instant triggers* (a true event-driven pipeline). Build the webhook trigger, if wanted, on that foundation rather than twice. (Consumer OneDrive can technically do change-notification subscriptions on subfolders, but they expire + need auto-renewal — a silent-failure risk we chose not to add pre-AEP. The app's local `watchdog` watcher covers the app side already.)
16. **Image-upload intake (accept phone photos).** Right now the whole pipeline assumes `.pdf` (the app uploader, the folder-watch filters, scan detection, archive naming) — but the extraction engine reads a photo fine (Claude vision, not traditional OCR). Cleanest approach: **normalize to PDF at the front door** (convert image → PDF on upload in Roundabout / at intake) so nothing downstream changes. Must handle **HEIC** (iPhone's default) — needs a conversion lib. Manual workaround for now: agents gallery-import the photo into **CamScanner** → it outputs a PDF → drop that in like any scan (worth a line in the Help section). The email-body-photo route is a separate, larger piece that belongs with the **Jillian** build, not this engine. *(Verified Sept 10: a phone-photo CIS converted to PDF ran through the pipeline and correctly landed in Pending Review — e.g. Margaret Klein.)*
17. **Productize: replace n8n with a standalone app (post-AEP, own planning session).** Long-term direction if MuddLX ships this to other agencies. n8n is scaffolding — great for proving it works (done), not what you'd hand to strangers. Two distinct visions: **(1) Consolidate own setup** — a Python service replacing the n8n orchestration (triggers, Graph calls, routing) for Jordon's four agents, still OneDrive-backed; a medium rebuild that kills the n8n dependency + the per-agent-workflow copying. The Roundabout app (already Python, already the front-end) is a head start. **(2) Real product for other agencies** — a hosted cloud backend + real database + file storage (drop OneDrive-as-database), user accounts, per-agency data isolation, billing, updates, support, AND **HIPAA compliance + BAAs** (handling other agencies' clients' health data on MuddLX infrastructure — a legal/business effort, not just code; ties to #15). Vision 2 is company-building, not a refactor. Either way: **after AEP**, and big enough to deserve its own planning session. For this AEP, n8n stays — rebuilding pre-deadline trades a working system for risk.

---

### ═══ AUGUST 2026 (historical — original plan) ═══

#### The two tracks (keep them separate)

**1. This project — the internal engine.** The tool Lacey, Jenna, Jacob, and Jill use: drop in a client's info, get a file back. Full drug-cost comparison, all the detail. Internal-only, so none of the client-facing compliance/legal weight applies. **This is the track being sharpened for AEP.**

**2. Client-facing plan-comparison PDF — Phase 3, lives in the Jillian build.** Reuses this engine later, but the sending, compliance, and legal send-window all stay in the Jillian chat/doc — not built or discussed here. For the record: the client-facing version has **no provider info** — plan cost + summary of benefits only, "basically what medicare.gov would show them." *(Sept note: this is now roadmap item #14 above, and is being brought into this app's scope rather than kept solely in the Jillian build.)*

---

#### Where we left off (Aug 2026)

The pipeline is production-built and sat idle for months. Docs were just reconciled from the retired Make.com build to the live n8n build (see Current Architecture above). That reconciliation surfaced four open items, now logged in the doc:

- ✅ **Extract with Claude prompt — FIXED (Aug 29)** — was truncated at "…specialty, clin"; full prompt restored in the live node and verified end-to-end on a real handwritten SOA (custom_plans + flags + providers all populate).
- ⚠ **No error alerting** — the Make-era Pushover/error handler never carried over to n8n. *(Sept note: a global Error Workflow — CIS Pipeline - Error Handler — now emails on failure; the remaining gap is the app-visible Failed-file writer on the main path, roadmap #6.)*
- ⚠ **Workflow is inactive** (`active: false`). *(Sept note: the pipeline is active and running on its poll timer.)*
- Confidence threshold is **0.7** (old docs wrongly said 0.75 — now corrected everywhere).

---

#### The order (small → big)

**1. Fix the truncated prompt. ✅ DONE (Aug 29, 2026).** The complete prompt was pasted into the Extract with Claude node and verified end-to-end on a real handwritten SOA — custom_plans captured "HP Journey Steady" off a margin note, flags fired (incl. an amlodipine over-dose catch), providers behaved. Live node now matches the SOA doc. (See the flags-banner check under step 2.)

**2. Full audit + refresher.** Claude walks the whole system with MCP access — live n8n workflow, Railway repo, R2 databases, the reconciled docs — and produces a plain-language map of how it works *today* plus a health check (what's solid / stale / broken). Purpose: re-onboard Jordon and establish true state before changing anything. *(Sept 19: DONE — see the Sept 19 block up top.)*

*Specific things to check during this audit:*
- **Do `flags` render as a PDF warning banner?** On the Aug 29 prompt-fix verification, Claude correctly flagged "amlodipine 20mg exceeds standard max dose" in the extraction JSON, but the generated report showed amlodipine as Tier 1 with **no ⚠ drug-verification banner**. So flags are now being *generated* (n8n side fixed) but may not be *rendered* on the PDF — a Railway/ReportLab (`main.py`) question. Confirm whether banners render at all and which flag types trigger them. *(Sept 19: root-caused — the `flags` array is never sent to the engine; see the Sept 19 block. ✅ Sept 21: FIXED — n8n now sends `flags` and the engine renders them as the ⚠ Drug Verification banner, verified on a real flagged sheet; Goal B fully closed. See the September 21, 2026 Session Detail.)*

**3. Data refresh / AEP readiness.** `quarterly_refresh.py` has **never been run end-to-end** against real CMS files. This is the real AEP dependency — the engine is only as good as the plan/formulary data under it, and 2027 data lands in the fall. Prove the refresh works before AEP. *(Sept note: now roadmap #13 with a full plan.)*

**4. Sharpen intake + reliability.** Add error alerting, decide on activating the workflow, and make the "team drops a file → gets a PDF" path robust and obvious. *(Sept note: largely addressed by the Phase 8 review loop + Roundabout app + Human-Readable Filenames; remaining reliability items are roadmap #6–#8.)*

---

#### Open question (for steps 2/4 to settle)

"Put a PDF somewhere, get output" **already exists** — the CamScanner → OneDrive `/Incoming/` → `/Reports/` flow. So the real question is:

- Make the *existing* intake more robust and obvious for the team, **or**
- Add a *second* intake shaped around **client-information sheets** (not SOAs)?

Related external dependency: Jill's **Client Information Sheet** doesn't exist yet. Whatever it becomes, it must capture at minimum the client's **drug list + zip code** for the engine to run. Better to tell Jill what the sheet needs to capture than to reverse-engineer around whatever she already uses. *(Sept note: settled — the Client Information Sheet now exists and is the intake; the Roundabout app is the robust, obvious "drop a file → get a PDF" front end.)*

---

#### Not in scope here *(Aug framing — partially superseded)*

- Client-facing PDF generation, auto-drafting, compliance, legal send window → **Jillian / Phase 3** (separate chat + doc). *(Sept note: the client-facing comparison is now planned as part of this app — roadmap #14 — though compliance/send-window still overlap with the Jillian work.)*
- Roundabout (human-in-the-loop review app) → *(Sept note: no longer parked — it's built and live; the review loop and finished-report retrieval both run through it.)*

---

## What This Does (Plain English)

An agent scans a handwritten Medicare Scope of Appointment form with CamScanner and drops the PDF into a OneDrive folder. Within about a minute, the system automatically reads the form, looks up every drug the client takes across all available Medicare plans in their area, and generates a professional two-page PDF report showing exactly what each plan would cost them annually. No manual lookups. No data entry. No medicare.gov.

**Time saved per client:** 25–30 minutes of manual medicare.gov lookups  
**Built on:** n8n (self-hosted, v2.19.5) + Railway (Python/Flask) + Claude API + CMS Medicare Data  
**Target market:** Small Medicare insurance agencies in Minnesota (expandable to other states)

---

## Architecture Overview

```
OneDrive /Incoming/
    ↓  (OneDrive polling trigger — checks every minute)
n8n Workflow (self-hosted, v2.19.5)
    ↓
Claude API (reads PDF, extracts client info + drug list as JSON)
    ↓
Route by Confidence (IF node)
    ↓ High (≥0.7)                     ↓ Low (<0.7)
Count → Build filename           Copy to /Needs Review/
Rename SOA → Log to Sheets        Delete from /Incoming/
Call Railway API                      (stop)
    ↓
Claude API (normalizes drug names — fixes spelling, brand→generic)
    ↓
RxNav NIH API (looks up drug identifiers)
    ↓
CMS Formulary Database (tier levels, copays, monthly costs)
    ↓
Provider Directory DBs (Medica, BCBS, HealthPartners, Humana, UHC, Aetna)
    ↓
ReportLab PDF Generator (2 pages)
    ↓
OneDrive /Reports/ (drug plan comparison PDF)
OneDrive /Processed/ (renamed original SOA)
```

---

## OneDrive Folder Structure

```
SOA Automation/
├── Incoming/       ← Agent drops CamScanner PDFs here
├── Processed/      ← Renamed SOA originals after successful processing
├── Needs Review/   ← Low confidence SOAs that need a human to check
└── Reports/        ← Generated drug cost comparison PDFs
```

---

## n8n Workflow — Node Map

**Workflow name:** `SOA Automation Workflow` · **n8n:** v2.19.5 (self-hosted, Docker) · **Instance:** https://n8n.muddlx.com

**17 nodes.** The high-confidence path runs the full pipeline; the low-confidence path diverts the SOA to /Needs Review/ and stops.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | Watch Incoming Folder | OneDrive Trigger | Polls /Incoming/ (every minute) and emits new PDFs |
| 2 | Download PDF | OneDrive | Downloads the file as binary (`fileId = {{ $json.id }}`) |
| 3 | Extract with Claude | Anthropic (Analyze Document) | Reads the PDF → structured JSON (client + drugs + providers) |
| 4 | Parse JSON | Code | `JSON.parse()` on Claude's text output (n8n does not auto-parse) |
| 5 | Route by Confidence | IF | `confidence ≥ 0.7` → high path; otherwise → low path |
| 6 | Count Existing Runs | Google Sheets | Counts prior rows matching First + Last + SOA Date (dup counter) |
| 7 | Aggregate Runs | Aggregate | Collapses matching rows into one item so the counter can read them |
| 8 | Build Filename | Code | Builds `filename` (SOA) + `reportFilename` (Drug Run) with dup suffix |
| 9 | Rename a file | OneDrive | Renames the original SOA in place to the formatted name |
| 10 | Log to Sheets | Google Sheets | Appends the full extracted record to the SOA Tracker sheet |
| 11 | Build Railway Body | Code | Assembles + `JSON.stringify()`s the request body (incl. providers) |
| 12 | Call Railway API | HTTP Request | `POST /process-soa`; returns the PDF as a file |
| 13 | Upload Report | OneDrive | Uploads the Drug Run PDF to /Reports/ |
| 14 | Copy to Processed | OneDrive | Copies the renamed SOA into /Processed/ |
| 15 | Delete a file | OneDrive | Deletes the original from /Incoming/ (completes the "move") |
| L1 | Copy a file | OneDrive | **LOW PATH** — copies the SOA into /Needs Review/ |
| L2 | Delete from Incoming (Low Confidence) | OneDrive | **LOW PATH** — deletes the SOA from /Incoming/, then stops |

**Flow order:**
Watch → Download → Extract with Claude → Parse JSON → **Route by Confidence**
- **High (≥0.7):** Count Existing Runs → Aggregate Runs → Build Filename → Rename a file → Log to Sheets → Build Railway Body → Call Railway API → Upload Report → Copy to Processed → Delete a file
- **Low (<0.7):** Copy a file (→ /Needs Review/) → Delete from Incoming → stop

> The low path does **not** rename or log — the original CamScanner file is moved to /Needs Review/ untouched for a human to inspect.

### OneDrive Folder IDs (personal account — use "By ID" mode)

| Folder | ID |
|--------|-----|
| Drive ID | `DD237822D09C059B` |
| Incoming | `DD237822D09C059B!s46b8b820fbcc41d497601f16d4be9e71` |
| Needs Review | `DD237822D09C059B!sd8847b2da7ab4c7e9c43de00e698b977` |
| Processed | `DD237822D09C059B!s25e189eb95cc4beda1f864ff7312261b` |
| Reports | `DD237822D09C059B!secc82c0b724848a6851ada3c9ca7eadf` |

> **Why Copy + Delete instead of Move:** n8n's OneDrive node has no Move operation on personal accounts. Archiving is therefore Copy-to-destination followed by Delete-from-source (two nodes) — used on both the high path (→ Processed) and the low path (→ Needs Review).

---

## Extract with Claude — n8n Anthropic Node

**Node:** Extract with Claude · **Type:** Anthropic (LangChain) — Analyze Document · **Resource:** Document
**Model:** `claude-sonnet-4-6` · **Input:** Binary (from Download PDF) · **Simplify:** off · **Credential:** Anthropic API

The downloaded PDF is passed as binary; the prompt below is the instruction. The node returns Claude's raw text, which the **Parse JSON** code node then parses.

> ✅ **RESOLVED (Aug 29, 2026).** The live prompt was previously truncated mid-sentence at `… specialty, clin`, dropping the rest of the providers instruction, the `custom_plans` margin-scanning instruction, and the `flags` instruction. The full prompt below was restored in the Extract with Claude node and verified end-to-end on a real handwritten SOA: `custom_plans` captured "HP Journey Steady" off a diagonal margin note, `flags` fired five items (including an over-max-dose catch on amlodipine 20mg), and `providers` behaved correctly. The live node now matches the prompt below.

```
CRITICAL: Return only a valid JSON object. No explanation, no markdown, no code blocks. Start your response with { and end with }.

You are processing a Medicare Scope of Appointment form for an insurance agency. Extract all fields exactly as they appear. Return null for any field that is missing or illegible.

{
  "client_first_name": "",
  "client_last_name": "",
  "date_of_birth": "MM/DD/YYYY",
  "address": "",
  "city": "",
  "state": "",
  "zip_code": "",
  "phone_number": "",
  "soa_date": "MM/DD/YYYY",
  "appointment_date": "MM/DD/YYYY",
  "coverage_types": [],
  "drugs": [
    {
      "name": "",
      "dosage": "",
      "frequency": "",
      "is_injectable": false
    }
  ],
  "providers": [
    {
      "raw_text": "",
      "last_name": "",
      "first_name": "",
      "specialty": "",
      "clinic_name": "",
      "city": ""
    }
  ],
  "custom_plans": "",
  "signature_present": true,
  "confidence": 0.0,
  "flags": []
}

For coverage_types extract all items listed or checked such as Medicare Advantage, Part D Drug Coverage, Medicare Supplement, etc.

For providers extract every doctor, dentist, specialist, or clinic listed anywhere on the form. For each entry: raw_text is the exact text as written on the form. Parse out last_name, first_name, specialty, clinic_name, and city where you can; leave any sub-field blank if it is not written. Do not invent providers.

For custom_plans scan the entire page including handwritten notes at the top, in margins, or any blank space. Look for requests to compare specific plans using phrases like "also check", "compare", "add", "include", "what about", "check". If you see any handwritten note that appears to reference a plan name — even if partially illegible — make your best attempt to extract it as a comma-separated string. Do not leave custom_plans blank just because the handwriting is unclear. Examples: "BC Comfort, Medica Preferred" or "HP Journey Steady". Return an empty string "" only if there are absolutely no margin notes or plan requests anywhere on the page.

For flags note anything unusual such as: Signature missing, Date missing or illegible, Any section appears incomplete, Handwriting unclear on specific fields.
```

> **Note:** the `providers` block and the tail of the providers instruction shown above are reconstructed from the intended design — verify the exact wording when you restore the prompt.

### Parse JSON (Code Node)

n8n does not auto-parse the Anthropic node's output the way Make.com did. A Code node converts Claude's text to JSON before anything downstream can read it:

```javascript
const rawText = $input.item.json.content[0].text;
const parsed = JSON.parse(rawText);
return [{ json: parsed }];
```

---

## Build Railway Body + Call Railway API

The request body is assembled in a **Build Railway Body** Code node and `JSON.stringify()`'d into a single `body` field, which the HTTP Request node then sends. This is the n8n workaround for the HTTP Request node mangling arrays (the `providers` array) in raw JSON-body mode — build and stringify the whole body first, then pass it through.

### Build Railway Body (Code Node)

```javascript
// Reads the extraction handed in by the caller (high path or resume path)
// via the When Called trigger. Same 'report math' for both paths.
const data = $('When Called').first().json;
const drugNames = data.drugs.map(d => d.name).join(',');
const drugDosages = data.drugs.map(d => d.dosage).join(',');
const clientName = `${data.client_first_name} ${data.client_last_name}`;
const providers = data.providers || [];
const flags = data.flags || [];

return [{ json: {
  body: JSON.stringify({
    client_name: clientName,
    dob: data.date_of_birth,
    zip_code: data.zip_code,
    soa_date: data.soa_date,
    drug_names: drugNames,
    drug_dosages: drugDosages,
    client_address: data.address,
    client_city: data.city,
    client_state: data.state,
    confidence: data.confidence,
    custom_plans: data.custom_plans,
    providers: providers,
    flags: flags
  })
} }];
```

Unlike the old Make.com body, drugs are **not** capped at 12 positional slots — `drugs.map().join(',')` handles any number of drugs, and `providers` is passed as a real array.

> **Updated Sept 21, 2026:** the body now also sends the `flags` array (added alongside `providers`, shown above) so Claude's extraction flags reach the engine and render in the report's ⚠ Drug Verification banner — this closed the flags-banner gap and completed Goal B. The whole block above was confirmed against the live node (Sept 21) and now matches it exactly, including that it reads the extraction from the shared **CIS Engine** sub-workflow's **When Called** trigger (`$('When Called').first().json`), not the old `$('Parse JSON')` reference — both the main and resume paths call this one node, so the report math lives in a single place.

### Call Railway API (HTTP Request Node)

| Setting | Value |
|---------|-------|
| Method | `POST` |
| URL | `https://web-production-dcce3.up.railway.app/process-soa` |
| Send Headers | on — `Content-Type: application/json` (set manually) |
| Send Body | on — Specify Body: **JSON** |
| JSON Body | `{{ $json.body }}` (expression — the pre-stringified body from the Code node) |
| Response | Response Format: **File** (Railway returns the PDF as binary) |
| Auth | none (public endpoint) |

> **Two n8n gotchas baked into this pair:** (1) arrays in the HTTP Request node's JSON-body mode serialize as `[object Object]`, so the body is stringified in the Code node and passed as `{{ $json.body }}`; (2) `Content-Type: application/json` is **not** added automatically — it must be set manually under Send Headers. The response is captured as a file so the PDF binary flows straight into Upload Report.

---

## Build Filename (n8n Code Node)

Builds two names from the parsed data plus the duplicate counter: `filename` (the renamed SOA) and `reportFilename` (the generated Drug Run PDF). The duplicate suffix is now ` (2)`, ` (3)` — not the old `_2` — and it applies to **both** names.

```javascript
const allRows = $('Count Existing Runs').all();
const matchCount = allRows.filter(item => Object.keys(item.json).length > 0).length;

const firstName = $('Parse JSON').item.json.client_first_name;
const lastName = $('Parse JSON').item.json.client_last_name;
const soaDate = $('Parse JSON').item.json.soa_date;

const formattedDate = soaDate.replace(/\//g, '-');
const suffix = matchCount === 0 ? '' : ` (${matchCount + 1})`;

const filename = `${firstName} ${lastName} - SOA - ${formattedDate}${suffix}.pdf`;
const reportFilename = `${firstName} ${lastName} - Drug Run${suffix}.pdf`;

return [{ json: { filename, reportFilename } }];
```

Examples (SOA / report):
- First run: `Jack Reacher - SOA - 02-05-2026.pdf` / `Jack Reacher - Drug Run.pdf`
- Second run: `Jack Reacher - SOA - 02-05-2026 (2).pdf` / `Jack Reacher - Drug Run (2).pdf`
- Third run: `Jack Reacher - SOA - 02-05-2026 (3).pdf` / `Jack Reacher - Drug Run (3).pdf`

> The match count comes from **Count Existing Runs** (a Google Sheets lookup on First + Last + SOA Date) fanned through **Aggregate Runs**. Empty-object items are filtered out so a zero-match lookup counts as 0. This replaces the old Make.com Data Store counter. Count Existing Runs has **Always Output Data** on and **Retry On Fail** on.

---

## Log to Sheets (n8n Google Sheets Node)

**Operation:** Append · **Spreadsheet:** SOA Tracker (`1UyMcZOci7GJEhl6KCFGp0arYjCm22-tZxJ-WVW2_3XM`) · **Sheet:** Sheet1 (`gid=0`)

Mapping mode is "Map each column manually." Field expressions read from the **Parse JSON** node; filename and path fields read from **Build Filename** and the **Watch Incoming Folder** trigger. 19 columns:

| Column | Value |
|--------|-------|
| Date Processed | `={{ $now.toFormat('MM/dd/yyyy') }}` |
| Client First Name | `={{ $('Parse JSON').item.json.client_first_name }}` |
| Client Last Name | `={{ $('Parse JSON').item.json.client_last_name }}` |
| Date of Birth | `={{ $('Parse JSON').item.json.date_of_birth }}` |
| Address | `={{ $('Parse JSON').item.json.address }}` |
| City | `={{ $('Parse JSON').item.json.city }}` |
| State | `={{ $('Parse JSON').item.json.state }}` |
| Zip Code | `={{ $('Parse JSON').item.json.zip_code }}` |
| Phone Number | `={{ $('Parse JSON').item.json.phone_number }}` |
| SOA Date | `={{ $('Parse JSON').item.json.soa_date }}` |
| Appointment Date | `={{ $('Parse JSON').item.json.appointment_date }}` |
| Coverage Types | `={{ $('Parse JSON').item.json.coverage_types.join(' \| ') }}` |
| Medications | `={{ $('Parse JSON').item.json.drugs.map(d => d.name).join(' \| ') }}` |
| Signature Present | `={{ $('Parse JSON').item.json.signature_present }}` |
| Confidence Score | `={{ $('Parse JSON').item.json.confidence }}` |
| Flags | `={{ $('Parse JSON').item.json.flags.join(' \| ') }}` |
| Original Filename | `={{ $('Watch Incoming Folder').item.json.name }}` |
| Processed Filename | `={{ $('Build Filename').item.json.filename }}` |
| File Path | `={{ $('Watch Incoming Folder').item.json.path }}` |

> **Column names must already exist in the sheet's header row** for append mapping to land in the right place. `Client First Name`, `Client Last Name`, and `SOA Date` are also the three columns **Count Existing Runs** filters on for the duplicate counter — keep those names in sync between the two nodes. Only the high-confidence path logs; low-confidence SOAs are never written to the sheet.

---

## Railway API Reference

**URL:** `https://web-production-dcce3.up.railway.app`  
**GitHub Repo:** `MuddLX/medicare-drug-engine`  
**Plan:** Hobby ($5/month)
**Server settings (Oct 2):** Railway's Custom Start Command (`bash -c "python startup.py && gunicorn app.main:app"`) overrides the Procfile. Concurrency comes from the Railway variable `GUNICORN_CMD_ARGS = --workers 3 --timeout 180 --graceful-timeout 30`. `INTERNAL_REPORT_V1=1` switches the internal report back to the previous layout.

| Endpoint | Method | What It Does |
|----------|--------|-------------|
| `/process-soa` | POST | Full pipeline — takes client + drug + provider data, returns PDF |
| `/health` | GET | Confirms DB is connected and returns row counts |
| `/drug-costs` | POST | JSON drug cost lookup for testing |
| `/plans` | GET | Lists all plans in the database |
| `/debug-costs` | POST | Returns plan selection debug info |
| `/plans-for-zip` | GET | *(Oct 2026)* `?zip=55441` → every eligible MA + Part D plan for the ZIP's county (no SNP/PACE/Medigap), for the app's plan picker |
| `/client-comparison` | POST | *(Sept 2026)* Client-facing one-page plan comparison PDF |

> **`selected_plans` (Oct 2026):** `/process-soa` and `/client-comparison` both accept an optional ordered list `"selected_plans": [{"contract_id":"H2001","plan_id":"116"}, …]`. Pick order = column order. Limits: 7 MA + 3 PD (internal); the client sheet shows the first 3 MA + first PD. Bad picks return a 400 with a plain-English `error`. Send `plan_year` too so the internal report prices the full plan year.

**Test command (PowerShell):**
```powershell
$body = @{
    client_name="Dorothy Hansen"; dob="03/22/1951"; zip_code="55113"
    soa_date="05/10/2026"; drug_names="Eliquis,lisinopril,Jardiance"
    drug_dosages="5mg,10mg,25mg"; client_address="1842 County Rd B"
    client_city="Roseville"; client_state="MN"; confidence="0.82"
    providers=@(
        @{raw_text="Dr Smith Minneapolis"; last_name="Smith"; first_name="John"; specialty="FAMILY PRACTICE"; clinic_name=""; city="Minneapolis"},
        @{raw_text="HealthPartners Roseville"; last_name=""; first_name=""; specialty="FAMILY PRACTICE"; clinic_name="HealthPartners Medical Group"; city="Roseville"},
        @{raw_text="Dr. Tran dentist Maplewood"; last_name="Tran"; first_name="Bao"; specialty="DENTIST"; clinic_name=""; city="Maplewood"}
    )
} | ConvertTo-Json -Depth 5
$response = Invoke-WebRequest -Uri "https://web-production-dcce3.up.railway.app/process-soa" -Method POST -ContentType "application/json" -Body $body -UseBasicParsing
[System.IO.File]::WriteAllBytes("C:\Users\Mudd\Desktop\test_output.pdf", $response.Content)
```

---

## Railway Project Structure

```
medicare_drug_engine/
├── app/
│   ├── __init__.py
│   └── main.py              ← Flask API (all plan logic, provider lookups, PDF generation)
├── startup.py               ← Downloads all DBs from R2 on every Railway startup
├── validate_db.py           ← DB quality checks (run standalone anytime)
├── quarterly_refresh.py     ← Master quarterly data refresh script
├── README_REFRESH.md        ← Step-by-step refresh instructions
├── r2_check.py              ← Verify DB is in Cloudflare R2
├── build_aetna_providers.py ← Builds aetna_providers.db from NPPES bulk data
├── build_uhc_db.py          ← Builds uhc_providers.db from myAARPMedicare PDFs
├── build_bcbs_db.py         ← Builds bcbs_providers.db from BCBS PDFs
├── build_hp_db.py           ← Builds hp_providers.db from HealthPartners PDFs
├── build_humana_db.py       ← Builds humana_providers.db from Humana PDFs
├── build_medica_db.py       ← Builds medica_providers.db from Medica FHIR API
├── FUTURE_IMPROVEMENTS.md  ← Documented gaps and planned future work
├── diagnose_nppes.py        ← NPPES diagnostic (column headers + sample Allina rows)
├── diagnose_nppes2.py       ← Deep NPPES scan (full file, MN Allina rows, Parent LBN values)
├── upload_aetna_db.py       ← Uploads aetna_providers.db to R2
├── Allina NPPES/            ← NPPES bulk zip (NPPES_Data_Dissemination_May_2026_V2.zip, 1.05GB)
├── UHC Provider PDF's/      ← Source PDFs for UHC provider DB
├── Procfile
├── requirements.txt         ← flask, gunicorn, requests, reportlab, boto3
├── runtime.txt
└── nixpacks.toml
```

**Railway Start Command:** `bash -c "python startup.py && gunicorn app.main:app"`

**Environment Variables (Railway):**
| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `R2_ACCESS_KEY_ID` | Cloudflare R2 credentials |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 credentials |
| `R2_ENDPOINT_URL` | Cloudflare R2 endpoint |
| `R2_BUCKET_NAME` | `medicare-db` |
| `MISE_PYTHON_GITHUB_ATTESTATIONS` | `false` — required workaround for Railway/mise build bug |

---

## Database & Infrastructure

### Storage Architecture
The database (`medicare_mn.db`, ~92MB) is stored in **Cloudflare R2** (free tier, 10GB). Railway downloads it automatically on every startup via `startup.py`. The DB is NOT stored in GitHub — only code lives there.

**Why R2 instead of GitHub:** GitHub has a 100MB file size limit. R2 is free, has no meaningful size limit, and zero egress cost.

### Databases in R2

| File | Contents | Size |
|------|----------|------|
| `medicare_mn.db` | CMS formulary + pharmacy + plan data | ~92MB |
| `medica_providers.db` | Medica provider directory (~25,000 providers) | ~5MB |
| `bcbs_providers.db` | Blue Cross provider directory (~24,000 providers) | ~5MB |
| `hp_providers.db` | HealthPartners provider directory (~43,000 providers) | ~9MB |
| `humana_providers.db` | Humana provider directory (~23,000 providers) | ~6MB |
| `uhc_providers.db` | UHC provider directory (7,720 providers) | ~2MB |
| `aetna_providers.db` | Allina Health Aetna facility directory (279 facilities) | ~0.1MB |

### Database Tables (medicare_mn.db)

| Table | What's In It |
|-------|-------------|
| `plans` | All 65 MN plans — name, premium, deductible, formulary ID |
| `formulary` | Every drug covered by each plan — tier level, prior auth, step therapy |
| `beneficiary_cost` | Copay amounts per tier at preferred/standard/mail order pharmacy |
| `pricing` | Average monthly cost per drug per plan |
| `pharmacy_network` | Which pharmacies are in-network for each plan (by NPI number) |
| `pharmacy_names` | Pharmacy names, addresses, and GPS coordinates |
| `service_area` | Which plans are available in which MN counties |
| `zip_county` | Maps zip codes to MN counties (888 MN zips) |
| `zip_coords` | GPS coordinates for every MN zip code |

### Plan Coverage
The system knows about **65 Medicare Advantage and Part D plans** across all major MN carriers:
HealthPartners, Blue Cross, Medica, Humana, Aetna/Allina, UHC/AARP, Align, and all Part D standalone plans.

Plans are selected dynamically based on the client's zip code → county → which plans CMS says are available there. The 5 cheapest/best plans for that county show by default.

### Provider Directory Databases

| Carrier | Source | Providers | Build Script |
|---------|--------|-----------|-------------|
| Medica | CMS FHIR API (`flex.optum.com/fhirpublic/R4`) | ~25,000 | `build_medica_db.py` |
| Blue Cross MN | Published PDF directories | ~24,000 | `build_bcbs_db.py` |
| HealthPartners | Published PDF directories | ~43,000 | `build_hp_db.py` |
| Humana | Published PDF directories | ~23,000 | `build_humana_db.py` |
| UHC (H2001) | myAARPMedicare.com People Directory PDFs | 7,720 | `build_uhc_db.py` |
| Allina Health Aetna (H3219) | NPPES CMS bulk NPI data (May 2026) | 279 | `build_aetna_providers.py` — facility-level only ⚠️ TO REVISIT |

**Provider databases do NOT auto-refresh** with quarterly CMS data. They must be manually rebuilt annually when carriers update their directories. UHC PDFs should be re-downloaded from myAARPMedicare.com each fall after open enrollment.

---

## How Plan Selection Works (Plain English)

> **Superseded Oct 2026:** the agent now picks the plans (see the ⭐ October 2026 box in Current Architecture). The ranking below only runs when a request has no `selected_plans` (legacy / backward compatibility).

1. Client's zip code gets looked up → resolves to a Minnesota county
2. The database checks which of the 65 plans CMS says serve that county
3. Plans are grouped by carrier (one per carrier family)
4. For each carrier, the cheapest plan wins. If two plans tie at $0, lower deductible wins
5. The 5 best plans across different carriers show as the default columns
6. If the agent wrote "also check HP Steady" on the SOA, that plan gets added as a 6th column
7. If a requested plan isn't available in that county, a warning banner explains why

---

## Custom Plan Requests (Agent Feature)

> **Retiring Oct 2026:** handwritten plan requests are replaced by the in-app plan picker. `custom_plans` is ignored whenever `selected_plans` is sent.

Agents can handwrite plan requests anywhere on the SOA — top of page, margins, wherever there's space. Claude reads the whole page looking for phrases like "also check", "compare", "add", "what about".

**How it works:**
- Claude extracts the request as plain text (e.g. "HP Journey Steady")
- The system fuzzy-matches it against all 65 plans using carrier aliases (BC → Blue Cross, HP → HealthPartners) and plan keywords (comfort, core, pace, stride, etc.)
- If it matches and is available in the client's county → added as an extra column (max 2 extras, 7 columns total)
- If it matches but isn't available in that county → ⚠️ Plan Request Notice banner in the report

---

## PDF Report Sections

**Header:** Client name (large), DOB/Zip/SOA date, INTERNAL USE ONLY badge, confidence score. Warning banners appear here for drug issues or unavailable plan requests.

**Section 1 — Plan Overview**
Side-by-side comparison of up to 7 plans. Shows monthly premium, drug deductible, estimated annual drug cost, and total (drug + premium). All plans are shown equal — the ★ "best plan" marker and highlighted column were removed in the Sept 19, 2026 refresh.

**Section 2 — Drug Formulary Tiers**
Grid showing which tier each drug is on for each plan. Color coded: Tier 1 = green, Tier 2 = blue, Tier 3 = amber, Tier 4 = red. Shows the brand name in parentheses when a drug was written as a brand name but looked up as generic.

**Section 3 — Pharmacy Cost Comparison**
Shows the nearest in-network pharmacies (up to 4, laid out two-across as of the Sept 19, 2026 refresh) with distances and per-pharmacy monthly cost, the deductible-phase breakdown, and mail order pricing with annual savings. On ZIP-only intake (no street address) distances fall back to the ZIP centroid.

**Section 4 — Part D Standalone Plans**
Same format as Section 1 but for standalone drug plans only. Shows 3 plans from different carriers, sorted by total annual cost.

**Page 2 — Provider Network Directory**
Shows which of the client's doctors/dentists are in-network for each carrier. Columns: Medica · Blue Cross · HealthPartners · Humana · UHC. Color coded: green = In Network, red = Not Found, gray = N/A (e.g. Medica has no dental benefit). Footer includes carrier phone numbers and verification reminder.

**Design:** Page 1 fits on one landscape A4 page even with 6 columns and 8 drugs. Printer-friendly white background with slate blue-gray (#1e293b) headers and teal accents (Sept 19, 2026 refresh — previously warm charcoal).

---

## Warning Banners

> **Updated Sept 23, 2026:** the banner is now compact and grouped: **Medications to verify** / **Form notes** / **Plan requests**, one row per group with items separated by " · ". Claude's extraction flags are sorted into medication vs form notes; SOA-era flags (signature, SOA date, appointment date, coverage types) are dropped; plain words replace the ⚠ glyph (the report font can't draw it). The description below is the original (pre-Sept 23) design, kept for history.

Two types of warning banners appear at the top of the report when needed:

**⚠ Drug Verification Required** — appears when Claude flags something about a drug:
- Unusual dose (e.g. amlodipine 20mg — max is usually 10mg)
- Brand name interpreted as generic
- Drug couldn't be identified confidently

**⚠ Plan Request Notice** — appears when an agent-requested plan can't be added:
- Plan not available in the client's service area
- Plan name couldn't be matched

---

## Drug Normalization Pipeline (Plain English)

Raw drug names from handwritten SOAs are messy — misspellings, brand names, nicknames, incomplete names. Before any lookup happens, every drug goes through:

1. **Claude API** — fixes spelling, maps brand names to generic (Xarelto → rivaroxaban, Lantus → insulin glargine), handles nicknames ("water pill" → furosemide), flags unusual doses
2. **RxNav NIH API** — looks up the official drug identifier (RxCUI) so the formulary lookup is exact

The report shows both the original name and the interpreted name (e.g. "rivaroxaban 20mg (written: Xarelto 20mg)") so agents can verify.

---

## Duplicate SOA Handling

If the same client's SOA gets processed twice (agent uploads it twice by mistake), the system handles it gracefully:

- **Count Existing Runs** (a Google Sheets lookup) counts how many prior rows in the SOA Tracker match this client's First + Last + SOA Date
- **Build Filename** adds a ` (n)` suffix based on that count — applied to **both** the renamed SOA and the generated report:
  - First run: `Jack Reacher - SOA - 02-05-2026.pdf` / `Jack Reacher - Drug Run.pdf`
  - Second run: `Jack Reacher - SOA - 02-05-2026 (2).pdf` / `Jack Reacher - Drug Run (2).pdf`
  - Third run: `Jack Reacher - SOA - 02-05-2026 (3).pdf` / `Jack Reacher - Drug Run (3).pdf`

Because the report filename now carries the same suffix, /Reports/ no longer relies on OneDrive's auto-rename — each run lands as its own uniquely-named file. (This replaces the old Make.com Data Store counter and the `_2` suffix format.)

---

## Quarterly Data Refresh

CMS releases updated Medicare plan data 4 times a year. This needs to be run to keep drug costs, formulary tiers, and pharmacy data current.

**Release schedule:**
| Quarter | Approximate Release Date |
|---------|------------------------|
| Q1 | Late January |
| Q2 | Late April |
| Q3 | Late July |
| Q4 (+ new plan year) | Mid October ← most important |

**Your job each quarter (5 minutes of actual work):**

1. Watch for CMS email notification (subscribe at `cms.gov/subscribe` → Part D updates)
2. Download **SPUF quarterly zip** from `cms.gov` (the big formulary/pricing/pharmacy file)
3. Download **Landscape CSV** from `cms.gov` (premiums and deductibles — small file)
4. Drop both files into `C:\Users\Mudd\medicare_drug_engine\refresh_input\`
5. Run: `python quarterly_refresh.py` from that folder
6. Wait for Pushover notification (takes 30–45 min, mostly geocoding)
7. Verify the report still looks right by running a test SOA

**What the script does automatically:**
- Extracts and processes all SPUF files
- Rebuilds all database tables
- Looks up pharmacy names from the NPI registry
- Geocodes pharmacy addresses (Census API)
- Rebuilds zip → county mappings
- Validates the new database before deploying
- Backs up the current database
- Uploads new database to Cloudflare R2
- Pushes to GitHub (triggers Railway redeploy)
- Verifies Railway came up correctly
- Runs a test SOA for Jack Reacher
- Sends Pushover notification with results

**If something goes wrong:** The script never touches the live database until everything passes validation. Rolling back is as simple as running `git revert` or restoring from the backup file.

**Full instructions:** See `README_REFRESH.md` in the project folder.

**Important:** Provider databases (medica, bcbs, hp, humana, uhc) are NOT rebuilt during quarterly refresh. They require manual rebuilding annually or when carriers publish updated directories.

---

## Known Issues & Limitations

| Issue | Details |
|-------|---------|
| MN only | All plans are Minnesota-specific. Expanding to other states requires a new plan list and DB rebuild |
| ⚠ No error alerting configured | The workflow has **no error workflow** attached — a failed node notifies no one. Only Count Existing Runs has Retry On Fail. The old Make "master error scenario + Pushover" did not carry over to n8n. **Priority gap to close.** |
| ⚠ Workflow currently inactive | The exported workflow has `active: false` — it is not listening as-is. Activate it before relying on it in production. |
| ✅ Extract with Claude prompt — RESOLVED (Aug 29) | Was truncated at `… specialty, clin`, dropping the providers tail + custom_plans + flags. Full prompt restored in the live node and verified end-to-end on a real handwritten SOA (custom_plans + flags + providers all populate). |
| Archiving uses Copy + Delete | n8n's OneDrive node has no Move operation on personal accounts, so archiving is Copy-to-destination then Delete-from-source (two nodes on both the high and low paths). Not a bug — just how personal OneDrive behaves in n8n |
| CMS data is quarterly | Prices reflect the most recent quarterly file. Can be up to 3 months behind current rates |
| Polling trigger (not instant) | The n8n OneDrive Trigger polls /Incoming/ about once a minute — not a true push/instant trigger, but near-real-time. (The Make→n8n migration is done; a webhook-based instant trigger remains a possible future nicety.) |
| ~$3 pricing gap vs medicare.gov | Our steady-state drug costs are typically within $0.20–$3.50 of medicare.gov per pharmacy. The gap is caused by pharmacy-specific negotiated unit prices above the MFP floor — data not publicly available from CMS. Our prices are accurate to public CMS data. See "Data Sources & Pricing Accuracy" section for full explanation |
| quarterly_refresh.py untested end-to-end | Script is written but hasn't been run against real CMS files yet. First real test will be Q3 release (late July 2026) |
| Aetna provider directory — facility-level only ⚠️ TO REVISIT | 279 Allina Health MN facility records (clinics, hospitals) built from NPPES. Cannot match individual doctor names — NPPES stores Allina physicians under parent org NPI without individual links. Lookup matches on facility name + city. Better solution: Aetna FHIR developer portal (free registration at developerportal.aetna.com) or Oct 2026 CMS MAPROVIDERS.JSON mandate. |
| UHC dental coverage | Only 3 dental clinics in the UHC DB. Supplemental dental network (Solera/DentaQuest) not in public PDFs. See FUTURE_IMPROVEMENTS.md |
| UHC provider count ⚠️ TO REVISIT | 7,720 providers vs ~23,000–43,000 for other carriers. Limited by myAARPMedicare.com 1,000-result cap per search. Coverage solid for Twin Cities metro and 78 of 87 MN counties. Better solution needed — investigate Aetna FHIR portal, CMS MAPROVIDERS.JSON mandate (Oct 2026), or fresh PDF download strategy. |

---

## Error Handling

> ⚠ **This is the biggest production gap.** The Make.com setup had a master error-handling scenario that emailed and sent a Pushover alert on any failure. **That did not carry over to n8n.** As exported, the n8n workflow has:

- **No error workflow attached** (`errorWorkflow` is unset) — if any node throws, the execution just fails in the background and nobody is notified.
- **Retry On Fail** on exactly one node: **Count Existing Runs** (the Google Sheets lookup). Every other node uses default behavior (fail the run, no retry).
- **No dead-letter / alerting** — a SOA that fails mid-pipeline is not moved anywhere special or flagged for a human. It can be left sitting in /Incoming/.

**What a production-grade setup should add (recommended next work):**
1. Create a dedicated **error workflow** in n8n (Error Trigger node → a notification node — email, Telegram, or Pushover) and attach it under **Settings → Error Workflow**.
2. Decide what happens to a SOA that fails mid-pipeline — e.g. move it to /Needs Review/ or a new /Failed/ folder so it isn't silently lost from /Incoming/.
3. Consider **On Error → Continue (using error output)** on the OneDrive archive nodes so a single archive hiccup doesn't fail an otherwise-successful run.

Google Sheets logging (**Log to Sheets**) is confirmed working on the high-confidence path — all successful runs are logged with full client and drug data. Low-confidence SOAs are not logged.

---

## Future Enhancements

- [ ] **Configure an n8n error workflow + alerting** (email/Telegram/Pushover) — replaces the retired Make error scenario (near-term priority)
- [x] **Restore the full Extract with Claude prompt** (providers tail + custom_plans + flags) and re-test extraction — ✅ done & verified Aug 29, 2026
- [ ] Optional: true instant trigger via webhook instead of the ~1-minute OneDrive poll
- [ ] Dedicated "Agent Notes" section on SOA form for cleaner custom plan requests
- [ ] OOP max ($2,100 in 2026) added to plan comparison
- [ ] Expand to other states (requires new plan config + DB rebuild per state)
- [ ] Telegram bot notification for agent when report is ready
- [ ] Web portal — React frontend where agents can enter data directly without a PDF
- [ ] GoodRx API integration for retail cash price comparison
- [ ] UHC dental network — investigate Solera Dental/DentaQuest directory (Q3 2026)
- [ ] **Aetna provider directory — individual doctors** — register for Aetna FHIR developer portal (developerportal.aetna.com, free, OAuth client credentials) OR wait for Oct 2026 MAPROVIDERS.JSON mandate
- [ ] **UHC provider directory** — investigate better source for individual doctor names statewide (FHIR portal, Oct 2026 mandate, or new PDF strategy)
- [ ] October 2026 MAPROVIDERS.JSON mandate — rebuild UHC + Aetna provider DBs from CMS machine-readable files when mandate kicks in

See `FUTURE_IMPROVEMENTS.md` for full details on each provider-related item.

---

## Data Sources & Pricing Accuracy

This section explains exactly where every number in the report comes from, why our prices may differ slightly from medicare.gov, and what agents should know when utilizing this tool.

---

### Where the Data Comes From

The report is built entirely from **official CMS (Centers for Medicare & Medicaid Services) public data files**, the same source that powers medicare.gov. We download these files quarterly and load them into a local database.

**CMS SPUF (Standard Plan Use File) — Quarterly**
The main data file. Released 4 times per year. Contains:
- Every drug covered by every Medicare plan in the country (formulary)
- Which tier each drug is on (Tier 1–6)
- Copay amounts per tier at preferred vs non-preferred pharmacies
- Every pharmacy in every plan's network (by NPI number)
- Per-pharmacy dispensing fees for brand and generic drugs
- Average monthly drug costs per plan

**CMS Landscape File — Annual (updated at AEP)**
Published each fall for the upcoming plan year. Contains:
- Monthly premiums for every plan in every county
- Drug deductible amounts per plan per county
- Which plans are available in which counties
- Plan type (PPO, HMO, HMO-POS, etc.)

**CMS ZIP→County Crosswalk — Census Bureau**
Maps every US zip code to its county. Used to determine which plans CMS says are available at the client's address.

**NPI Registry — HHS**
The national database of all healthcare providers including pharmacies. Used to look up pharmacy names and addresses from their NPI numbers.

**Nominatim / OpenStreetMap**
Used to geocode pharmacy addresses to GPS coordinates so we can calculate real driving distances from the client's home.

**Provider Directories — Carrier PDFs and FHIR APIs**
Each carrier's in-network provider list. Sources vary by carrier — see Provider Directory Databases table above.

---

### How We Calculate Drug Costs

**Step 1: Identify the drug**
Claude reads the drug name from the SOA (including misspellings and brand names) and maps it to the correct generic name and RxCUI identifier using the RxNav NIH API.

**Step 2: Look up the formulary tier**
We query the formulary database to find which tier the drug is on for each plan. Tier determines the copay structure.

**Step 3: Apply the correct copay logic**

| Situation | What We Do |
|-----------|-----------|
| Insulin drugs | Apply the 2026 federal $35/month insulin cap (IRA law) |
| MFP drugs (Xarelto, Eliquis, Jardiance, etc.) | Apply 25% coinsurance of the CMS Maximum Fair Price |
| Tier 1–2 generics | Apply the plan's flat copay from CMS beneficiary_cost data |
| Tier 3–4 brand drugs | Apply the plan's flat copay or coinsurance rate |
| Specialty (Tier 5) | Apply plan's specialty copay |
| Tier 6 (e.g. Align) | Apply $0 preferred tier rate |

**Step 4: Add pharmacy dispensing fee**
Each pharmacy charges a small per-fill fee on top of the drug cost. These fees are different per pharmacy and come directly from the CMS SPUF pharmacy network file. Brand drugs use the `brand_fee_30` rate; generics use `generic_fee_30`. This is why CVS might show $85.25/mo while Walgreens shows $84.42/mo for the same drug on the same plan — the dispensing fees differ.

**Step 5: Apply deductible phase**
If the plan has a drug deductible (e.g. $615), the client pays the full negotiated drug price until the deductible is met, then switches to the plan copay. We calculate this from the SOA date forward through December. The report shows each month where the cost changes (e.g. "May $232 → Jun $188 → Jul $84.42 steady").

---

### Why Our Prices May Differ from Medicare.gov

This is the most important thing for agents to understand. Our prices come from the same CMS data source as medicare.gov — but there is one layer of pricing data that CMS does not make publicly available.

**What causes the gap:**

Medicare Advantage plans negotiate contracts directly with each pharmacy. These pharmacy-specific contracted rates are transmitted through the Medicare Transaction Facilitator (MTF) — a private transactional system that CMS uses to process MFP refunds. This data is not published in any public CMS file.

Our prices use the **Maximum Fair Price (MFP)** — the federally negotiated ceiling price — plus the pharmacy's published dispensing fee. Medicare.gov has access to the actual contracted price that pharmacy charges that specific plan, which may be slightly above the MFP ceiling.

**In practice, the gap is small:**

| Pharmacy | Our Price | Medicare.gov | Difference |
|----------|-----------|-------------|------------|
| Walgreens (Rivaroxaban 2.5mg) | $49.42/mo | $52.46/mo | +$3.04 |
| Cub Pharmacy | $49.45/mo | $52.46/mo | +$3.01 |
| Coborns | $49.25/mo | $41.82/mo | -$7.43 |
| CVS | $58.85/mo | ~$52.46/mo | +$6.39 |

Walgreens and Cub are within $3 of medicare.gov. Coborns is actually cheaper than our estimate — they have a lower negotiated unit price below the MFP floor. CVS is higher because of their larger dispensing fee structure.

**What this means for agents:**
- Our prices are directionally correct and comparable between plans
- The relative ranking of plans (which is cheapest, which is most expensive) is accurate
- For specific dollar amounts, direct clients to medicare.gov or the plan's formulary tool for exact figures
- The report footer says "Verify before presenting" for this reason

**What we cannot get:**
- Per-pharmacy per-NDC contracted prices (MTF transactional data — not public)
- Year-to-date deductible accumulation (we don't know what the client spent Jan–SOA date)

---

### Pharmacy Network Accuracy

**How we determine if a pharmacy is in-network:**
We match the pharmacy's NPI number against the plan's pharmacy_network table in our database. This comes directly from the CMS SPUF file — the same data that determines network status on the plan's own website.

**Why different plans show different pharmacies:**
Each plan contracts with a different set of pharmacies. CVS may be in-network for Blue Cross but out-of-network for Medica. We only show pharmacies whose specific NPI is confirmed in that plan's network file.

**How we calculate distances:**
We geocode every pharmacy address using Nominatim (OpenStreetMap) to get GPS coordinates, then calculate the straight-line distance from the client's home address. All 1,856 MN pharmacies in our database have real GPS coordinates — no approximations.

**Preferred vs non-preferred pharmacies:**
Some plans (Aetna, Align, some Part D plans) distinguish between preferred and non-preferred pharmacies with different copay rates. When a plan makes this distinction, we label non-preferred pharmacies with "(non-pref)" so agents know. Plans like HealthPartners and Blue Cross use a flat copay at all in-network pharmacies — no preferred/non-preferred distinction.

---

### What the Tier Colors Mean

| Color | Tier | Meaning | Typical Cost |
|-------|------|---------|-------------|
| 🟢 Green | Tier 1 | Preferred generic | $0–$10/mo |
| 🔵 Blue | Tier 2 | Generic | $5–$20/mo |
| 🟡 Amber | Tier 3 | Preferred brand | $35–$100/mo |
| 🔴 Red | Tier 4 | Non-preferred brand | $75–$200/mo |
| 🟣 Purple | Tier 5 | Specialty drug | $100–$500+/mo |
| 🟢 Green | Tier 6 | $0 preferred (Align structure) | $0/mo |

A drug showing green regardless of its tier number means the copay is $0 — the color always reflects what the client actually pays, not just the tier number.

---

### What Agents Should Tell Clients

**"Where do these numbers come from?"**
"These prices come from the same CMS database that powers medicare.gov — the official government source. We pull it every quarter when CMS releases updated data."

**"Why is this different from what I saw on medicare.gov?"**
"Our prices are from the same source but may differ by a few dollars because medicare.gov has access to the exact price each pharmacy has negotiated with the plan, which isn't in the public data. Our numbers will be within a few dollars of the actual cost — close enough to compare plans accurately. For the exact dollar amount, we always recommend verifying at medicare.gov or calling the plan directly before enrolling."

**"How current is this data?"**
"CMS releases new data every quarter — January, April, July, and October. We update our database each time. The report always shows which quarter's data was used."

**"Are these pharmacies actually in-network?"**
"Yes — we verify each pharmacy against the plan's official network file from CMS. If a pharmacy appears in the report, it's confirmed in-network for that plan."

**"Is my doctor in network?"**
"Page 2 of the report shows your providers checked against each carrier's 2026 directory. Green means confirmed in-network. Red means not found — but always verify directly with the carrier before enrolling, as directories change throughout the year."

---

## Build History

| Date | What Was Built |
|------|---------------|
| April 27–28, 2026 | Complete initial build — Make scenario, Railway API, CMS database, PDF generator |
| April 29, 2026 | Header spacing fixes; "written:" label logic fixed; deductible phase math replaced with accurate flat copay rates; cost_type=0 bug fixed; injectable drugs excluded with "Verify coverage" display |
| May 5–6, 2026 | Major session — see below |
| May 9–10, 2026 | R2 storage, quarterly refresh infrastructure — see below |
| May 10, 2026 | Production testing, pharmacy geocoding, tier color system — see below |
| May 15, 2026 | Pharmacy accuracy deep dive, per-pharmacy pricing, NPI fix — see below |
| May 22–23, 2026 | UHC provider directory, page 2 provider network column — see below |
| June 2, 2026 | Aetna provider directory (279 Allina Health MN facilities from NPPES); Aetna column on page 2 — see below |
| July 30, 2026 | Documentation reconciled to the live n8n build (from Make.com) using the exported workflow JSON as source of truth — see below |
| August 29, 2026 | Restored the truncated Extract with Claude prompt in the live node; verified end-to-end on a real handwritten SOA (custom_plans + flags + providers) — see below |
| September 19, 2026 | Engine health check (Goal A — all green) + agent-report visual refresh (Goal B pt.1): slate palette, best-plan highlight removed, two-up pharmacy grid, section spacing; plus repo hygiene — see below |
| September 21, 2026 | Flags → PDF banner plumbing closed — **Goal B fully done**: n8n **Build Railway Body** now sends the `flags` array; the engine reads it and merges into the ⚠ Drug Verification banner. Proven engine-side (fake-flags POST) then end-to-end on a real flagged sheet — see below |
| October 2, 2026 (evening) | Messy fake sheet (Harold Lindgren) test; engine `a944eeb` bolder report section bands + `21dd8d1` readable reader notes / illegible doctors never "In network" / no-strength callout / no contract number in plan names; n8n extraction prompt v3 (never guess, `crossed_out`, `field_notes`) + Build Review JSON v3 (flagged_fields from notes + deterministic checks); app Phases 22–25 specified (pinned selection, blue Edit + Save confirmation, Plans tab room, Details cards with must-fix vs check + remove drug) — see the evening block in the October roadmap update |
| October 2, 2026 (afternoon) | App Phases 13–21 (new layout, freeze fix, colour rules, client cards, test-data reset); n8n claim-on-pickup + 15 s polling + Error Workflow + rewritten extraction prompt; engine 3 workers, picker detail, **one-table internal report** with Part D + doctors, injectables safety net, doctor "Possible match" honesty — see the afternoon block in the October roadmap update and the session detail below |
| October 1–2, 2026 | **Architecture change: agent-chosen plans** (Jill meeting). Engine: `/plans-for-zip`, `selected_plans` on both reports, full-plan-year cost period for the internal report, Cost plans shown, "Not identified" in Section 2, Part D columns on the client sheet. Verified offline (22 tests) and live on Railway. App (Roundabout Phase 12) + n8n changes in progress — see the October 2026 update in the roadmap and the October 2, 2026 Session Detail |
| September 23, 2026 | Client-facing sheet live + polished end-to-end on both paths (logo, half-stars, commas, official plan names, UnitedHealthcare, auto-fit, one-page guarantee, honest "to confirm" drugs); resume-path emit proven; internal warnings banner compacted into groups; Roundabout Phase 11 built; go-live order locked and folder restructure designed + deferred. See the Sept 23 block in the roadmap and the Phase 3 doc |

---

### May 5–6, 2026 Session Detail

**Fixed: Wrong plans showing for clients outside Hennepin County**
The database only had 217 zip→county mappings. Zip 55309 (Big Lake/Sherburne County) returned "NOT FOUND", so the engine fell back to hardcoded default plans that were wrong for that area. Rebuilt the zip_county table using Census Bureau data — now has 888 MN zip codes mapped correctly. Blue Cross Comfort now correctly shows $54.70 for Sherburne County clients.

**Fixed: Plan tie-breaking**
When two plans both had $0 premium, the wrong one was winning. Added logic: if premiums are equal, pick the plan with the lower drug deductible. This means HealthPartners Journey Pace ($350 deductible) correctly beats Journey Smart ($400 deductible).

**Fixed: Stale dollar amounts in plan display names**
Plan names had hardcoded prices baked in (e.g. "Blue Cross Comfort ($18)") from an earlier version when premiums were different. These were wrong and would go stale every time CMS updated premiums. Removed all inline dollar amounts — plans now show clean names only (e.g. "Blue Cross Comfort").

**Fixed: HealthPartners plan name wrapping**
"HealthPartners Journey Stride" was too long to fit in a column header without wrapping, which caused layout issues. Removed "Journey" from all HP plan names since it's the same across all of them — now shows "HealthPartners Stride", "HealthPartners Pace", etc.

**New: Dynamic Part D plan selection**
Previously hardcoded to show two specific Part D plans regardless of the client's location. Now dynamically selects the 3 cheapest Part D plans available in the client's county, from 3 different carriers, sorted by total annual cost.

**New: Custom plan requests**
Agents can handwrite "also check HP Steady" or "compare BC Comfort" in the margins of the SOA. Claude reads the handwriting, the system fuzzy-matches the plan name against all 65 plans, and adds it as an extra column (up to 2 extras, 7 columns max). If the requested plan isn't available in that county, a warning banner explains why instead of silently ignoring it.

**New: Split warning banners**
Previously one single "Drug Verification Required" banner for all warnings. Now two separate banners — one for drug issues and one for plan request issues — so agents can quickly see what type of action is needed.

**Fixed: Page 2 bleed on 6-column reports**
6-column reports (5 default + 1 custom) were pushing the footer onto a second page. Fixed by tightening spacing throughout: HR divider spacing, section spacers, pharmacy row padding, footer HR padding, and section title leading. Everything now fits on one landscape A4 page.

**Improved: PDF header readability**
Client name bumped from 9pt to 14pt bold. DOB/Zip/SOA date line bumped from 5pt to 10pt with darker color (#334155 instead of light gray #64748b). Right-side header (INTERNAL USE ONLY, confidence score) also bumped for balance.

**New: SOA duplicate filename handling in Make**
If an agent uploads the same client's SOA twice, the second file would fail to move and get stuck in /Incoming/. Added a Data Store counter that appends _2, _3, etc. to the filename automatically. Files now always move successfully regardless of duplicates.

---

### May 9–10, 2026 Session Detail

**New: Cloudflare R2 storage for the database**
The database was previously stored in GitHub (92MB, approaching the 100MB limit). Moved to Cloudflare R2 — free object storage with no meaningful size limit and zero download cost. Railway now downloads the database from R2 on every startup via `startup.py`. The database is no longer tracked in git.

**Why this matters:** As a product scales, the database will grow with each quarterly refresh. GitHub would eventually hit its limit and fail. R2 eliminates that concern permanently and is the correct production architecture.

**New: Automated quarterly refresh system**
Built a complete automated refresh pipeline (`quarterly_refresh.py`) that replaces the manual multi-hour process of downloading CMS files, running multiple scripts, and pushing to GitHub. The new process takes 5 minutes of hands-on work (downloading 2 files and running 1 command) and handles everything else automatically.

The script:
- Auto-detects input files by scanning their contents (not filenames), so CMS naming changes don't break anything
- Builds to a temporary database — never touches the live database until everything passes
- Validates 9 quality checks before deploying (row counts, required plans, required drugs, zip→county accuracy, pharmacy coordinate coverage, DB size)
- Backs up the current DB both locally and to R2 with a timestamped filename
- Uploads new DB to R2
- Triggers Railway redeploy via git push
- Verifies Railway came back up with correct data (polls /health endpoint)
- Runs a full end-to-end smoke test (Jack Reacher SOA)
- Sends Pushover notification with success/failure summary

**New: DB validation script**
`validate_db.py` can be run standalone anytime to check database health. Useful after a refresh, after any DB changes, or when troubleshooting issues. Checks table presence, row counts, required plans, required drugs, zip lookups, geocoding coverage, and DB size.

**New: Startup validation**
`startup.py` runs before the Flask app on every Railway deploy. Checks R2 credentials, downloads the DB if missing or outdated, and validates it has all required tables before the app starts. If anything fails, the app refuses to start — preventing Railway from running with a corrupt or missing database.

### May 10, 2026 Session Detail

**Fixed: Pharmacy geocoding — switched from Census to Nominatim (OpenStreetMap)**
The Census geocoder was failing for ~100% of pharmacy addresses because it can't handle suite numbers and non-standard retail address formats. Switched to Nominatim (OpenStreetMap) which handles commercial addresses correctly. Hit rate went from 0% to 100% on the first run. All 1,856 MN pharmacies now have precise GPS coordinates.

**Fixed: Pharmacy distances were all showing the same value**
When a pharmacy had no GPS coordinates, the system fell back to the zip code centroid, making all pharmacies in the same zip appear equidistant. After the Nominatim geocoding run, all pharmacies have real coordinates. The system also detects zip centroid fallback and displays `~` before the distance to indicate it's approximate.

**Fixed: Co-located pharmacy deduplication**
Pharmacies at the same campus but with different names (e.g. "Sanford Health Pharm" and "SANFORD CLINIC NORTH") were both showing up. Added proximity-based deduplication using actual GPS distance between pharmacies — if two pharmacies are within 0.1 miles of each other, only the closest is kept.

**Fixed: Stale dollar amounts in DB carrier column**
The plans table had values like "Align ChoicePlus ($0)" baked into the database from an earlier build. Fixed by running an UPDATE to clean all stale dollar amounts from the carrier column.

**Fixed: UHC FG plan name wrapping**
"AARP Medicare Advantage from UHC FG-0001" was too long for a column header. Added "UHC AARP FG" to FRIENDLY_NAMES for the FG-series plans.

**New: Full 6-tier color system with legend**
Added Tier 5 (purple — specialty) and Tier 6 (green — $0 preferred). Added copay-aware coloring: any tier with a $0 copay displays green regardless of tier number. Added a legend row below Section 2 explaining all 6 tiers.

**New: PPO preference over HMO**
When multiple Medica plans are available in a county, PPO plans now rank above HMO-POS plans at equal premium. Medica H8889 (PPO) now correctly shows instead of H6154 (HMO-POS).

**Tested: Multi-county validation**
- Sherburne (55309) — Jack Reacher — HealthPartners, Blue Cross, Medica H8889 PPO
- Ramsey (55113) — Dorothy Hansen — UHC AARP, Aetna, Blue Cross, HealthPartners, Humana, Medica (6 carriers)
- Beltrami (56601) — Bemidji rural — Align ChoicePlus, Blue Cross, Medica, UHC AARP FG
- Olmsted (55901) — Rochester — Humana Gold Choice, Medica, Blue Cross Complete

---

### May 15, 2026 Session Detail

**Fixed: NPI prefix mismatch — pharmacy JOIN was completely broken**
The pharmacy_network table had 12-digit NPIs with a spurious `10` prefix (e.g. `101023030707`) while pharmacy_names had 10-digit NPIs (`1023030707`). This meant zero pharmacies were joining correctly — every SOA was showing zip centroid distances instead of real GPS distances, and pharmacy filtering was not working at all. Fixed with a one-time UPDATE that stripped the `10` prefix from all 54,497 rows. Now 52,652 pharmacies JOIN correctly.

**Why this was critical:** Without the JOIN, CVS was incorrectly showing as in-network for Medica even though CMS data shows it's out-of-network. Every pharmacy shown was just any pharmacy in that zip code, not pharmacies confirmed in that plan's network.

**Fixed: Per-pharmacy dispensing fees**
Previously all pharmacies showed the same price. After the NPI fix, each pharmacy's actual `brand_fee_30` and `generic_fee_30` from the CMS pharmacy_network file now applies. This means:
- Walgreens Rivaroxaban: $49.25 MFP + $0.17 brand fee = **$49.42/mo**
- CVS Rivaroxaban: $49.25 MFP + $9.60 brand fee = **$58.85/mo**
- Coborns: lower negotiated unit price = **$41.82/mo** (below MFP)

Brand drugs (Tier 3+) use `brand_fee_30`, generics use `generic_fee_30`.

**Fixed: Per-pharmacy pricing display**
Section 3 now shows each pharmacy's individual steady-state monthly cost. The cheapest pharmacy's deductible transition is shown in the monthly cost column. Runner-up pharmacies show their own post-deductible price.

**Fixed: Proximity dedup now uses GPS coordinates**
Previously, the proximity dedup used client distance — so CVS at 2.8mi and Walgreens at 3.0mi would sometimes knock each other out because they were close in client distance. Now uses actual GPS distance between pharmacies. Only genuinely co-located pharmacies (same building/campus) get deduped.

**Fixed: Non-preferred pharmacy label logic**
Plans like HealthPartners and Blue Cross mark all pharmacies as `preferred_retail = N` in CMS data — not because they're non-preferred, but because these plans use a flat copay at all pharmacies and don't distinguish preferred/non-preferred. We now detect whether a plan has ANY preferred pharmacies — if not, the "(non-pref)" label is suppressed since it would be meaningless and misleading.

**Updated: Section 3 disclaimer**
Changed from "All in-network preferred pharmacies charge the same copay" (no longer true with per-pharmacy pricing) to "Costs shown reflect CMS negotiated rates including pharmacy dispensing fees. Prices may vary by pharmacy."

**Pricing accuracy vs medicare.gov (Jack Reacher, 3 drugs, zip 55309):**
| Pharmacy | Our Price | Medicare.gov | Gap |
|----------|-----------|-------------|-----|
| Walgreens | $49.42/mo | $52.46/mo | $3.04 |
| Cub | $49.45/mo | $52.46/mo | $3.01 |
| CVS (HealthPartners) | $58.85/mo | ~$52.46/mo | $6.39 |

The remaining gap is pharmacy-specific negotiated unit prices above the MFP floor — not publicly available from CMS. This is the maximum accuracy achievable from public data sources.

---

### May 22–23, 2026 Session Detail

**New: UHC Provider Directory (7,720 MN providers)**

Built the UHC provider database from myAARPMedicare.com People Directory PDFs — a completely new data acquisition approach. After exhausting every API and FHIR endpoint (UHC's FHIR API only has 9,500 total PractitionerRole records nationally, yielding only 5 MN providers from the first 25 pages), discovered that myAARPMedicare.com generates downloadable provider directory PDFs per plan and zip code.

Downloaded 13 People Directory PDFs covering all 6 UHC MN Medicare Advantage plans across all MN regions. Built a PDF parser (`build_uhc_db.py`) to extract providers from the merged-text format (PDF extraction strips all spaces, requiring camelCase splitting and city name lookup tables). Result: 7,717 medical providers + 3 dental clinics, 123 unique cities, 78 of 87 MN counties.

**PDFs downloaded (saved to `UHC Provider PDF's/` folder):**
- FG-0001 / FG-0002 (rural MN): zip codes 55803 (Duluth area), 56301 (St. Cloud area), 56601 (Bemidji area)
- MN-0001 (Twin Cities metro): zip codes 55101 (Saint Paul), 55303 (Anoka), 55337 (Burnsville), 55441 (Plymouth)
- MN-0004 (southern MN): zip codes 55902 (Rochester), 56001 (Mankato)
- MN-0005 (metro): zip code 55101
- 13 Places Directory PDFs also downloaded — parsed for dental clinic entries (3 found)

**County coverage:** 78 of 87 MN counties. The 9 missing counties (Big Stone, Chippewa, Lac qui Parle, Lincoln, Lyon, Pipestone, Rock, Stevens, Yellow Medicine) are counties where UHC pulled out of the market in 2026 — confirmed via Fox9 reporting and CMS service area data.

**Why other approaches failed:**
- UHC FHIR API (`flex.optum.com/fhirpublic/R4`): Intentionally incomplete — only 9,500 PractitionerRole records nationally regardless of network filter, geographic filters ignored on most resource types, full national crawl overnight confirmed same result
- alphadog PDF server (`uhc.com/medicare/alphadog/`): Contains EOC and Summary of Benefits documents only — no provider directory PDFs for MN plans (confirmed by brute-force URL scan)
- myAARPMedicare.com directory generator: Initially accessible, but hit Rally/Optum login wall after downloads completed — all 13 PDFs downloaded before wall went up
- NPPES bulk download: Would give "accepts Medicare" not "in-network with UHC" — incorrect for our use case

**UHC dental coverage gap:** Only 3 dental clinics found across all 13 Places Directory PDFs. UHC's dental benefit for Medicare Advantage uses a supplemental network (Solera Dental/DentaQuest) not represented in these directories. Documented in `FUTURE_IMPROVEMENTS.md` for Q3 2026 investigation.

**New: Page 2 — Provider Network Directory**
Built a complete second page of the PDF report showing provider network status across all 5 carrier databases. Each of the client's providers is looked up against Medica, Blue Cross, HealthPartners, Humana, and UHC simultaneously. Shows ✓ In Network (green), ✗ Not Found (red), or — N/A (gray, e.g. Medica has no dental benefit). Dynamically shows only the columns for carriers present in that client's plan results. Footer includes carrier phone numbers for verification.

**New: UHC column on page 2**
UHC column header shows "UHC" (not "UHC AARP"). Footer includes "UHC: 1-844-867-3487 or myAARPMedicare.com". The page title reads "MEDICA & BLUE CROSS & HEALTHPARTNERS & HUMANA & UHC MEDICARE ADVANTAGE" when all 5 carriers are present.

**Plan label cleanup:** All "UHC AARP" labels throughout the PDF (Section 1, Section 2, Section 3) renamed to just "UHC" for cleaner presentation.

**Updated: startup.py**
Added `uhc_providers.db` to the R2 download sequence. Validates minimum 400 providers on startup. All 6 databases now download and validate on every Railway deploy.

**New: FUTURE_IMPROVEMENTS.md**
Created `FUTURE_IMPROVEMENTS.md` in the repo documenting the UHC dental gap, October 2026 MAPROVIDERS.JSON mandate opportunity, Allina Health Aetna provider directory gap, and general future enhancement list with timelines.

---

### June 2, 2026 Session Detail

**New: Allina Health Aetna Provider Directory (279 MN facilities)**

Completed the provider network directory by adding an Aetna column on page 2 — the last missing carrier. Extensively researched all available data sources before building:

**Sources researched and outcomes:**
- **Aetna FHIR developer portal** (`developerportal.aetna.com`) — Real API with bulk export for Medicare Provider Directory exists. Requires free developer account registration with Client ID/Secret (OAuth2, scope "Public NonPII"). Endpoint: `apif1.aetna.com/fhir/v1`. Deferred — registration out of scope for this session. Best long-term solution.
- **Aetna Transparency in Coverage MRF files** — Contains every NPI in the H3219 network but organized by billing code. Full Aetna MRF spans ~25 billion provider records across thousands of gzipped files — multi-terabyte, not practical for extraction.
- **Blue Cross provider directory PDFs** — Already in project. Contains hundreds of Allina Health clinic entries but only clinic-level (no individual doctor names). Not used.
- **AllinaHealthAetnaMedicare.com** — Online-only directory, explicitly copyright-protected. Not used.
- **NPPES CMS bulk download** — Free government open data. Scanned 9.5 million records, found 300 MN rows containing "Allina." Only 26 were individual doctors (Type 1 NPIs) — Allina physicians register under their own NPIs without a reliable parent org link, making individual doctor lookup from NPPES impossible. 274 were organization records (Type 2 NPIs) — clinics, hospitals, surgery centers.

**What was built:**
Downloaded the NPPES May 2026 bulk file (1.05GB zip, 9.5M records). Built diagnostic scripts (`diagnose_nppes.py`, `diagnose_nppes2.py`) to understand the data structure and confirm what Allina records actually look like. Built `build_aetna_providers.py` which scans the full file and extracts 279 verified Allina Health MN organization records using precise name matching to avoid false positives (excluded "Callina Healthcare", "United Hospital District", and rural community hospitals that partially matched our keywords).

**Data quality — 279 providers:**
- 105 clinics/urgent care centers, 70 hospital/facility records, 41 pharmacies, 14 internal medicine groups, 14 physician groups, and others
- Key cities: Minneapolis (64), Coon Rapids (24), Saint Paul (21), St Paul (14), Fridley (13), Buffalo (12), Plymouth (12) and 30+ others
- Key facilities confirmed present: Abbott Northwestern Hospital, United Hospital (St. Paul), Unity Hospital (Fridley), all Allina Health Surgery Centers, Allina Health Restorative Suites
- Source: NPPES May 2026 — CMS open government data, no restrictions

**Known limitation — facility-level only:**
Individual doctor names cannot be matched. NPPES stores Allina physicians under their own NPIs without a parent org link. When a SOA lists "Dr. Sarah Johnson at Allina Health Blaine Clinic," the system returns ✓ In Network because "Allina Health System · Blaine" is in the database, but cannot confirm the specific doctor. For most real-world SOA use cases (verifying the client's Allina clinic is in-network), this is sufficient. For individual doctor verification, revisit Aetna FHIR portal or Oct 2026 MAPROVIDERS.JSON mandate.

**Wired into main.py via patch script (`patch_aetna_providers.py`):**
13 surgical patches: AETNA_DB_PATH constant, `lookup_providers_aetna()` function, `show_aetna` detection, result list init, lookup call, entry building, `has_aetna_col` detection, active_carriers append, num_carrier_cols update, column header "Aetna", `aetna_col_idx` calculation, cell rendering with green/red background, carrier note in footer ("Aetna: 1-800-307-4830 or AllinaHealthAetnaMedicare.com").

**Updated: startup.py**
Added `aetna_providers.db` to R2 download sequence. Validates minimum 100 providers on startup.

**Railway deploy issue resolved:**
Build failed with `mise ERROR: No GitHub artifact attestations found for python@3.11.9`. Fixed by adding Railway environment variable `MISE_PYTHON_GITHUB_ATTESTATIONS=false`. This is a Railway/mise infrastructure bug unrelated to our code — document this env var as required.

**Final startup validation output (confirmed live):**
```
aetna_providers.db validation passed: 279 providers.
=== Startup complete. Starting app... ===
```

**Complete carrier scorecard — all 6 carriers live:**
| Carrier | Providers | Source |
|---------|-----------|--------|
| Medica | 25,817 | CMS FHIR API |
| Blue Cross | 24,725 | PDF parse |
| HealthPartners | 43,438 | PDF parse |
| Humana | 23,032 | PDF parse |
| UHC | 7,720 | myAARPMedicare.com PDF parse |
| Aetna | 279 | NPPES CMS bulk data (facility-level) |

---

### July 30, 2026 Session Detail

**Documentation reconciled to the live n8n workflow (was still Make.com-flavored)**

The project had drifted: this doc still described the Make.com scenario even though the build migrated to n8n months ago, and two copies existed (this vault master, plus a staler May 23 copy in the repo). Pulled the actual workflow (`SOA Automation Workflow`, 17 nodes) as an exported JSON and rewrote every orchestration section from it — node map, extraction prompt, Parse JSON, Build Railway Body + Call Railway API, Build Filename, Log to Sheets, duplicate handling, error handling, known issues, future enhancements — while leaving the platform-agnostic sections (Railway, databases, PDF, pricing) untouched.

**Findings surfaced by reading the real workflow (not memory):**
1. **Confidence threshold is 0.7**, not the 0.75 the old docs claimed (IF node: `confidence >= 0.7`).
2. **The Extract with Claude prompt is truncated** — it ends mid-sentence at `… specialty, clin`, so the providers-tail, `custom_plans` margin-scanning, and `flags` instructions are missing from the live node. Custom plans + flags are effectively not being extracted. Flagged in-doc; fix pending.
3. **No error workflow / alerting is configured** in n8n. The Make "master error scenario + Pushover" did not carry over; only Count Existing Runs has Retry On Fail. Biggest production gap.
4. **The workflow is currently inactive** (`active: false`).

**Also corrected:** filename duplicate suffix is now ` (2)` (not `_2`) and applies to both the SOA and the Drug Run report; Log to Sheets is a 19-column append (old doc showed 16); archiving is Copy + Delete (personal OneDrive has no Move); model is `claude-sonnet-4-6`; the drugs schema now includes `is_injectable`.

**Near-term priorities coming out of this:** (1) restore the full extraction prompt, (2) configure an n8n error workflow + alerting, (3) reactivate the workflow once those are in place.

---

### August 29, 2026 Session Detail

**Truncated Extract with Claude prompt restored + verified end-to-end**

The `Extract with Claude` prompt (truncated at "… specialty, clin" since before the July 30 doc reconciliation) was replaced in the live node with the complete prompt documented above. Verified on a real handwritten SOA (Jack Reacher, 55309) carrying a diagonal margin note requesting HealthPartners Journey plans:

- **custom_plans** captured `"HP Journey Steady"` off the margin note → Railway fuzzy-matched it → the report rendered the extra requested plan columns (Blue Cross Comfort, HP Journey Stride, HP Journey Steady). This is the field that was being silently dropped on every run before the fix.
- **flags** fired five items, including the drug-safety catch **"Amlodipine listed at 20mg which exceeds standard maximum dose of 10mg — possible transcription error."**
- **providers** returned `[]` correctly (no providers written on the form).
- Run came in at **0.72 confidence** — just over the 0.7 threshold; handwritten forms with margin notes ride close to the line.

**Observed (flagged to the AEP Readiness roadmap for the Step 2 audit, not fixed here):** the amlodipine over-dose flag was generated in the JSON, but the report showed amlodipine as Tier 1 with **no ⚠ drug-verification banner** at the top — so `flags` may not be rendering as a PDF banner. That is a Railway/ReportLab (`main.py`) question, separate from this n8n prompt fix, and needs confirmation. *(✅ Resolved Sept 21, 2026 — root-caused Sept 19 as "flags never reach the engine," then fixed both sides Sept 21: n8n sends `flags`, the engine merges them into the banner warnings; verified on a real flagged sheet. See the September 21, 2026 Session Detail.)*

**Also noted (roadmap Step 3):** the report still runs on **CMS Q1 2026 data** — the untested quarterly refresh remains the key AEP dependency.

---

### September 19, 2026 Session Detail

Two threads this session, both engine-side (the pipeline + Roundabout app were untouched): a full engine **health check** (roadmap "audit" step / Goal A) and the first half of the **agent-facing report redesign** (Goal B).

**Goal A - engine health check (all green).**
- Railway app confirmed up and serving, running commit `084460e` (June 2 code, untouched since). Confirmed `app/main.py` is the deployed entrypoint (Procfile: `web: gunicorn app.main:app`) and local repo == deployed (clean; `HEAD == origin/main == 084460e`).
- On a service restart, `startup.py` re-validated all 7 R2 databases with correct counts: medicare_mn.db (65 plans), medica (25,817), bcbs (24,725 / 3,334 dental), hp (43,438 / 3,885 dental), humana (23,032 / 11,720 dental), uhc (7,720), aetna (279). `/health` returned status ok, 65 plans, 888 zip-county rows, 1,587 service-area rows, zip 55309 -> Sherburne.
- **Data vintage confirmed:** core `medicare_mn.db` is CMS **Q1 2026** (R2-built May 18, 2026); provider DBs May-June 2026. Correct/expected for a pre-AEP health check - the 2027 refresh is the separate AEP dependency (roadmap #13).
- **Cost engine verified correct.** A ZIP-only test with all-generic drugs returned $0.00 across the MA plans; adding Eliquis (a brand/MFP drug) as a control returned real, differentiated costs with a correct month-by-month deductible ramp (e.g. HealthPartners $350-ded plan: Sep $231 -> Oct $119 -> Nov/Dec $57.75 steady = 25% of the MFP) plus per-pharmacy dispensing-fee differences. So the earlier all-zero result was legitimate (free Tier-1 generics), not a bug.
- **ZIP-only pharmacy fallback confirmed working.** With no street address, `get_client_coords` falls back to the ZIP centroid (`zip_coords`) - pharmacy pricing does NOT break on ZIP-only intake, it just drops to ZIP-level precision. (Resolves the roadmap "pharmacy breaks on ZIP-only" worry: it degrades, it doesn't break.)

**Flags-banner root cause found (the long-open "do flags render?" question).** The warning-banner code exists and works, but it only renders warnings the engine generates itself during drug lookup. Claude's extracted **`flags` array is never sent to the engine**: the n8n **Build Railway Body** node doesn't include `flags`, and `/process-soa` doesn't read a `flags` field. So the amlodipine-style dose catch has no path to the PDF at all. Fixing it needs BOTH sides - add `flags` to the n8n payload AND have the engine read them and merge into the warnings passed to `build_pdf`. Still pending (the remaining piece of Goal B).

**Goal B pt.1 - agent-report visual refresh (3 patches, live on Railway).** All changes are visual-only in `build_pdf` (`app/main.py`), applied via verify-or-abort patch scripts (`patch_pdf_refresh_1/2/3.py`), each syntax-checked before write and verified in rendered PDFs:
- **Patch 1** - slate palette (`CHARCOAL #1c1917 -> #1e293b`, `DARK_GRAY #292524 -> #334155`; drives headings, the dark section header bands, and body text) + removed the "best plan" star everywhere and the teal best-column highlight in the shared `make_plan_table` (Sections 1 & 4).
- **Patch 2** - Section 3 pharmacy list laid out two-across (2x2) instead of a tall stack (pharmacy column 62 -> 80mm, cost 88 -> 70mm; entries compacted - dropped "Pharmacy", whole-dollar price) + a 2mm breathing gap before Sections 2, 3, 4.
- **Patch 3** - removed the leftover best-plan highlight that Sections 2 & 3 build inline (each had its own `if ma_best in carriers:` teal/green highlight the shared-function fix in Patch 1 didn't touch). Caught by eyeballing the rendered PDF, not the code.
- Net: every plan is now shown equal (no star, no highlighted column/row), the palette is cohesive slate blue-gray, Section 3 is materially shorter, and the report still fits on one landscape page (with room to spare). Landscape orientation and font sizes were deliberately kept.

**Repo hygiene (engine repo).** `.gitignore` had been saved as **UTF-16**, so git couldn't read it and ignored nothing - leaving `.env` (R2 keys + Anthropic API key) one careless `git add .` away from being committed. Rewrote it as UTF-8 (`.env`, `*.db`, the big data `.txt` files, `_archive/`). Quarantined three dead `main.py`-shaped files (root `main.py`, `app/main_final_v2.py`, `app/main_header.py`) into `_archive/` so only the live `app/main.py` remains in the load path. (One stray `app/build_bcbs_db.py` dup left in place, noted.)

**Still open (carry forward):** the flags -> PDF plumbing (above) — ✅ CLOSED Sept 21, 2026 (see the September 21, 2026 Session Detail below); the 2027 data refresh (#13); provider matching still returns the wrong person on common surnames (last-name-only match -> false "In Network"), reconfirmed here and the reason to move to Lacey's provider-systems chart; the report footer hard-codes "CMS Medicare Formulary Q1 2026" as static text (it must be updated in lockstep with any data refresh or the report will lie about its own vintage); minor - `dist_approximate` renders false even on the ZIP-centroid fallback, and `requirements.txt` is unpinned (a benign urllib3 version warning at boot).

---

### September 21, 2026 Session Detail

**Flags → PDF Drug Verification banner — plumbing closed; Goal B fully done.**

The long-open flags-banner gap (root-caused Sept 19 as "Claude's `flags` array never reaches the engine") was fixed on both sides and proven end-to-end. It was done in the verify-in-isolation order — prove the engine renders flags first, *then* wire n8n to send them — so each half was independently confirmed before calling it done.

- **Engine side (`app/main.py`, deployed to Railway).** `/process-soa` now reads a `flags` field off the request body and merges those extraction flags into the warnings passed to `build_pdf`, so they surface in the existing ⚠ Drug Verification banner. Proven in isolation first: a direct POST carrying fake flags rendered the banner correctly.
- **n8n side (`Build Railway Body` node).** The node now includes the `flags` array in the payload it sends to `/process-soa` — previously it sent everything *except* flags, which was the exact gap. See the updated Build Railway Body code block above.
- **End-to-end proof.** A real sheet that fires a flag was run through the live pipeline and the ⚠ Drug Verification banner appeared on the generated report. **Goal B (the agent-facing report) is now fully closed** — the Sept 19 visual refresh plus flags now surfacing where an agent sees them before a client meeting.

**n8n publish note (workflow version history):** Version name — `Add flags to Railway payload → drug-verification banner`; Describe changes — `Build Railway Body node now sends the flags array to /process-soa, so Claude's extraction flags (e.g. dosing/safety warnings) surface in the report's Drug Verification banner. Pairs with the engine-side change already deployed to read + render them.`

**Still open after Goal B (unchanged):** the 2027 data refresh (#13); provider matching on common surnames (the last-name-only false-positive bug); the footer's hard-coded "CMS Q1 2026" vintage string; and the HIPAA/host-migration blocker (Railway can't sign an affordable BAA — test data only on Railway until the engine moves to a BAA host).

---

### October 2, 2026 Session Detail

**Context.** On Oct 1 Jordon and Jill decided agents should choose the plans instead of the system ranking them (full design: the ⭐ October 2026 box in Current Architecture). Target: working before the Saturday Oct 3 test with Lacey, fake data only. The Roundabout work went to Claude Code (Phase 12); the engine work was done directly on Jordon's engine folder through the linked computer.

**Engine changes (`app/main.py`, `app/client_comparison.py`, `app/client_pdf.py`) — commit `a446bee`:**
- Shared helpers so the picker and the reports can never disagree: `resolve_county` (returns whether the ZIP matched exactly or used the ±3 neighbor fallback) and `eligible_plan_rows` (the same MA/Cost and PDP queries the engine already used: no PACE/SNP, formulary required). `get_plans_for_zip` was switched to them and checked identical for all 891 ZIPs.
- `resolve_selected_plans` validates the agent's list (shape, duplicates, county eligibility, 7 MA / 3 PD limits) and builds the plan list in pick order, with unique labels when two plans share a friendly name.
- `compute_drug_costs(..., plans_override=...)` uses the agent's plans instead of the ranking.
- `GET /plans-for-zip` (new) and `selected_plans` on `/process-soa` + `/client-comparison`.
- Client sheet: `agent_selection` (first 3 MA + first PD, no ranking); Part D columns show "Not included" for medical/extra rows, "—" for doctors and star rating; carrier by contract number (`CONTRACT_CARRIER`); Part D plan-name cleanup ("AARP Medicare Rx Saver from UHC (PDP)" → "AARP Medicare Rx Saver (PDP)").
- Internal report: Cost plans now included in Section 1; Section 2 "Not identified" for unidentified drugs.
- `tests/test_agent_selected_plans.py` (19 tests at this commit), all offline (Claude, RxNav, and geocoding mocked).

**Cost-period fix — commit `bae6db1`:** the first live run showed the internal report pricing only October–December ($1,191 for UHC MN-0001) while the client sheet priced the full year ($2,114). Already in the engine before Oct 2, but it matters now that one pick drives both reports. `/process-soa` now uses `01/01/<plan_year>` as the cost start whenever `plan_year` is later than the SOA year; current-year requests are unchanged. Three new tests (fail before, pass after). Live re-run: internal $2,113.75 vs client $2,114 for UHC, and the other plans match too.

**Live verification:** `/plans-for-zip?zip=55441` (Jordon's browser) = 28 Hennepin plans, identical to local. `_scratch/live_check_selected_plans.py` (run from Jordon's PowerShell; the linked-computer shell can't reach Railway) passed all 4 checks.

**Gotchas learned:** commit from the linked computer with `git -c core.autocrlf=true` so CRLF files don't show whole-file diffs; the linked-computer shell has no route to Railway, so live checks run from Jordon's PowerShell; Jordon pushes with `git push origin main` from PowerShell.

**Next:** review Claude Code's Phase 12 report → n8n change (extraction-only) → end-to-end fake-sheet test → Lacey prep. See "Remaining before Saturday" in the October 2026 roadmap update.

### October 2, 2026 (afternoon) Session Detail
Summary lives in the roadmap block **"Oct 2, 2026 (afternoon) — Hardening, new layout built, new internal report"**. Additional working notes:
- **Testing approach that worked:** Jordon tested each app build hands-on with the fake sheets and fed back screenshots; Claude reviewed Claude Code's reports against the actual code on Jordon's machine (linked computer) and fixed engine issues directly. Two app bugs found this way (flashing windows; freeze) were root-caused before fixing.
- **Mockups first:** UI changes were agreed on the Design canvas "Roundabout — New Layout Mockup" (Upload, To Do, Completed Client Reports, Plans, and the internal report page) before any build prompt was written.
- **Engine commits:** `b16ea57` picker detail · `a6e82dc` one-table internal report · `2e7923d` Part D teal + doctor honesty + injectables · `98805bc` drop Part D divider + "excludes" note. Each pushed by Jordon with `git push origin main`.
- **Git on Jordon's machine:** commits made from the linked computer can leave `HEAD.lock` / `maintenance.lock` / `tmp_obj_*` behind (the linked shell can't delete without permission). Delete permission was granted for the engine folder and they were cleaned after each commit; otherwise Jordon's next push fails.

### October 2, 2026 (evening) Session Detail
Summary lives in the roadmap block **"Oct 2, 2026 (evening) — Messy-sheet test, flagged-field pipeline fixed, review UX queued"**. Working notes:
- **Master doc** updated mid-afternoon (commit `5c0dc5b`) and again this evening. Vault master and repo copy kept byte-identical.
- **Test sheets** live in Claude's workspace (`cis_test/`) and were sent to Jordon in chat: Ruth Anderson, Gerald Okafor, Linda Marchetti (typed, clean), **Harold Lindgren (messy, needs review)**. All fake data.
- **How the flagged-field gap was found:** Jordon's Details screenshots showed the orange set (DOB, ZIP, phone) didn't include the misspelled Lisinopril, the dose-less Metoprolol or the crossed-out Januvia, while the internal report's warning box listed all of them. Compared the live `Build Review JSON (19)` code (from the exported workflow) with the reader output → `flagged_fields` came only from `low_confidence_fields`. First fix (match paths inside `flags`) proved unreliable on the next run because the reader wrote plain sentences without paths → moved to structured `field_notes` + `crossed_out` + app-side severity.
- **Live n8n text** for nodes 6, 7 and 19 was pasted by Jordon before editing, so the v3 replacements were written against the exact live versions.
- Engine change verification: 47 offline tests (4 new: warning tidy + dedupe, illegible doctor → Possible match, no-strength callout, contract number stripped); one-page fit still holds for the 10-plan worst case.

