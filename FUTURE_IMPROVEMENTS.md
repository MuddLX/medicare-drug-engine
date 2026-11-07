# Open items, known gaps & future improvements
*Updated October 8, 2026 (evening). The May 2026 list (old carrier provider directories, UHC dental, MAPROVIDERS.JSON)
is in `_archive/FUTURE_IMPROVEMENTS_2026-05.md`; those provider databases were retired on Oct 6 in favour of
`providers_2027`.*


## ★ Update — October 31, 2026 (read first; the sections below are kept as written on Oct 8)
**Done since Oct 8**
- [x] **Oct 15 CMS drug lists + pharmacy networks loaded** (all 18 lists from CMS; per-pharmacy preferred flag). "Not
      covered" and pharmacy names/costs are on.
- [x] **2027 drug prices** — not waiting for January: real prices from **Medicare.gov Plan Finder** (Jordon's saved
      searches, `tools/mpf_load.py`): 330 labels, 62,852 prices on 46 plans (Oct 29). Missing drug/strength → left out
      and marked, never estimated.
- [x] **Yearly cost matches Medicare.gov's method** — incl. the Oct 29 $2,400-cap fix; `tools/verify_against_mgov.py`:
      60 of 63 fully priced searches within $5, deductible month 131/131.
- [x] Roundabout **Phase 43** (delete any sheet) and Phases 44–49 → **v0.3.1** (Submit opens the comparison; Save PDF
      choice; "Lowest total" only with 2+ complete plans).
- [x] Quartz + Align hidden from plan lists (`HIDDEN_CONTRACTS`).
- [x] n8n: List Incoming (2) retries 3× / 5 s; Watch Incoming Folder (3) accepts any PDF.

**1. Go-live blockers — still open (unchanged)**
- [ ] Move the engine off Railway to AWS (BAA). Then point Roundabout's `engine_base_url` and n8n at it.
- [ ] Access key on the engine endpoints (`/process-soa`, `/client-comparison`, `/compare-plans`, `/check-drugs`,
      `/plans-for-zip`).
- [ ] Street-address lookups (Census geocoder, OpenStreetMap) off or moved to AWS Location.
- [ ] n8n Bedrock API key (no expiry) — upgrade n8n + AWS credential, or an expiring key + reminder.
- [ ] AWS written confirmation that the BAA covers the Bedrock entrances used.
- [ ] Housekeeping from Oct 8 still listed in §1 below (delete `ANTHROPIC_API_KEY` on Railway, old n8n Extract node,
      Azure secret rotation, old `.env` copy in the claude.ai project) — check off when done.

**2. Known data gaps (Oct 31)**
| Gap | What agents see | Closes |
|---|---|---|
| Drugs/strengths not captured yet (outside ~top 250; e.g. pregabalin 50 mg + clotrimazole troche on Part D plans) | asterisk + "price not on file" — left out of that plan's total | fill searches 22–27 + later batches |
| Small-town stores | 92.9% of top drugs (Oct 28) | fill searches 22–25 |
| Search 22 (Bemidji) stops with "Could not tick pharmacy CVS…" even after the clear-on-new-ZIP fix (Oct 29, 6:19 pm) | small-town gaps stay open | diagnose the small-town pharmacy step, then re-run 22–25 |
| 4 plans whose search-21 capture failed (H2001-124, H3219-005, H5959-018, H8889-009 — Hy-Vee no rows) | older prices only | re-run search 21 for those plans |
| Pipestone Blue Cross H2461-010: yearly cost $80–$166 off Medicare.gov (first month) | — | investigate |
| Coverage Medicare.gov already shows but the CMS file doesn't yet (Xarelto, alprazolam, ergocalciferol on UHC; ketoconazole cream Part D copay) | tier/coverage per the Oct CMS file | November CMS monthly file |
| Brands priced "as generic" (Entresto, Brilinta, Januvia…) | "Tier N · as generic" (Medicare.gov: brand not covered) | by design — tell agents |
| OTC items (vitamin D 1000, fish oil, baby aspirin) | red "Not covered" + asterisk on every plan | planned: grey "OTC — not a Part D drug", no asterisk |
| Injectables (Ozempic, insulin pens…) | "Verify coverage", not priced | not planned yet |
| 2027 star ratings | "—" / "2027 rating not out yet" | CMS stars file |
| Realistic proof: all Medicare.gov comparisons so far were 11–48 drug capture lists | — | ~10 realistic blind searches |

**3. Next improvements (Oct 31)**
- [ ] OTC handling (above) + keep "units" for vitamin D doses.
- [ ] Realistic blind searches (5–10 drug lists, two that hit the cap) through `tools/verify_against_mgov.py`.
- [ ] Jill's sign-off: one sheet checked by hand on Medicare.gov (suggested UHC MN-0001 H2001-116 at CVS).
- [ ] Calcipotriene-type rows where the sheet writes the strength differently in name and dose still show the dose twice
      (cosmetic).
- [ ] January: decide whether CMS's quarterly prices replace or only fill gaps in the Medicare.gov prices.
- [ ] **More than 3 Part D plans (Jordon, Nov 5).** The review plan picker caps picks at 7 Medicare Advantage + 3 Part D
      (`src/engine/plans.py` MAX_MA, MAX_PD = 7, 3) because that is the internal PDF's layout limit
      (`src/report_runs.py` "The internal report holds up to 3 Part D plans"; engine `app/internal_pdf.py` columns).
      Raising it needs the engine's internal PDF to fit more Part D columns (or a second page) first, then the app limit.
      The Compare view's 6-plan limit is separate.

---
## 1. Must be done before any REAL client data (go-live blockers)
- [x] ~~AI calls to Bedrock~~ — **done Oct 8**: engine `normalize_drugs` and the n8n reading step (nodes 6a/6b) use
      Claude Sonnet 4.6 on AWS Bedrock (us-east-2) under the agency's AWS BAA. Verified live.
- [ ] **Move the engine off Railway to AWS** (Railway has no BAA). Likely App Runner or ECS in us-east-2; the databases
      can stay downloaded from R2 at start or move to S3. Then point Roundabout's `engine_base_url` and n8n's
      Call Railway API node at the new address. **This is the next big item.**
- [ ] **Get written confirmation from AWS** that the BAA covers the Bedrock entrances we use (asked in the Sonnet 5.5
      access email to `bedrock-ant-eap@amazon.com`, Oct 8).
- [ ] **n8n Bedrock key decision:** the n8n-bedrock **Bedrock API key never expires** (AWS calls long-term keys
      "exploration only"). Before real clients either (a) upgrade n8n to ≥2.26 (backup first) and switch node 6b to
      n8n's AWS credential with the n8n-bedrock access key, then revoke the API key; or (b) keep an API key with an
      expiry date + a calendar reminder to rotate it.
- [ ] **Lock the engine's endpoints.** Anyone with the URL can call `/process-soa`, `/client-comparison`,
      `/compare-plans`, `/check-drugs`, `/plans-for-zip`. Add an access key (header) that only Roundabout and n8n hold.
- [ ] **Address lookups:** the engine sends street addresses to the Census geocoder and OpenStreetMap (county choice,
      pharmacy distance). Turn off (`COUNTY_ADDRESS_LOOKUP=off`, city/ZIP only) or move to AWS Location before real
      clients.
- [ ] **2027 Star Ratings** loaded (README_REFRESH §5). Since Oct 8 nothing shows last year's stars (client sheet
      says "2027 rating not out yet"), so this is about completeness, not wrong data.
- [x] **Oct 15 CMS drug lists + pharmacy networks** loaded (README_REFRESH §6) — **done Oct 15–18** (★ Oct 31 update); checked against Medicare.gov for
      3–4 fake test clients. Turns on "Not covered" and pharmacy names automatically.
- [ ] **Pharmacy names for 2027 networks** (NPI Registry lookup for pharmacies new in 2027).
- [x] Roundabout **Phase 43** (delete any sheet from In Progress) + version bump to 0.2.0 + release build. — **Phase 43
      done; app now v0.3.1** (★ Oct 31 update). v0.3.1 (Phases 48–49) was a *working* build: run a release build (full
      test suite) before an exe goes to agents.
- [ ] Delete `ANTHROPIC_API_KEY` from Railway (unused since Oct 8); after a few days, delete the disconnected old
      `Extract with Claude` node in n8n.
- [ ] Rotate the Azure secret that was pasted in chat earlier (if not done).
- [ ] Remove the old `.env` copy from the claude.ai project (holds keys) — see project cleanup note.

## 2. Known data gaps (and when they close)
Since Oct 8 the engine **shows only official numbers as fact** (`app/availability.py`); each gap below shows as
"not available yet / verify" instead of an estimate, and switches on by itself when the data loads.
| Gap | What agents see today | Closes |
|---|---|---|
| 2027 drug prices (Medicare.gov uses plan-submitted prices CMS doesn't publish) | "Available January" — no dollar drug totals; each plan's official 2027 copay/coinsurance per tier IS shown | Jan 20, 2027 quarterly file |
| Drug lists from carrier PDFs (not CMS) | "Not on carrier's list — verify" (never "Not covered") | Oct 15 CMS lists |
| Humana 2027 drug list not published (Medica loaded Oct 8 from PDFs Jill supplied) | "Drug list not out yet" | Oct 15 CMS file |
| Pharmacy networks carried from 2026 | No pharmacy names / costs shown | Oct 15 CMS file |
| 2027 star ratings | "2027 rating(s) not out yet" | CMS, ~Oct 9–10 |
| Providers: only UHC (system level), Medica (2027 directory + system level) and agency confirmations | Other carriers "Not checked" | Weekly confirmations; carrier 2027 directories if Jill approves |
| ZIP→county uses Census 2020 ZCTAs | Rare edge ZIPs; street address lookup fixes most | — |
| No LIS / Extra Help, Medigap or SNP plans | Not in the plan list | not planned yet |

## 3. Next improvements (agreed or likely)
- [ ] **Sonnet 5.5 on Bedrock** once AWS lifts the account hold (mantle entrance: different URL/API — test with
      `_scratch\bedrock_check.py anthropic.claude-sonnet-5-5` before switching either the engine or node 6b).
- [ ] Check why the Testy McTestface report showed **Aetna Enhanced** when the margin note said "Aetna Signature"
      (agent pick, or custom-plan matching?).
- [ ] App-only setup (no n8n/OneDrive per agent): extraction endpoint on the BAA host; replaces per-agent n8n cloning.
- [ ] "Run again with different plans" (Completed Client Reports).
- [ ] Provider "covered as another brand" note: e.g. Basaglar is on no MN list, but other insulin glargine brands are.
- [ ] Audit log replacement for the retired n8n SOA Tracker sheet (app-side log, no client health data).
- [ ] Code signing (Azure Artifact Signing) so Smart App Control doesn't block the exe.
- [ ] n8n cleanup after a few days of real use (export first): the old disconnected Extract node, the old
      high-confidence chain, `Route by Confidence (8)`, `CIS Resume - After Review`, `CIS Engine - Report Back-Half`.
- [ ] Roundabout under version control (git) and point the update check at a real repo.
- [ ] Ask the FMO (via Jill) about ConnectureDRX / Sunfire quoting access for Plan Finder–grade prices before January.

## 4. Later / ideas
- Expand beyond Minnesota (new service areas + data per state).
- GoodRx-style cash price comparison.
- Agent notification when reports are ready (Telegram / Teams).
