# Open items, known gaps & future improvements
*Rewritten October 7, 2026. The May 2026 list (old carrier provider directories, UHC dental, MAPROVIDERS.JSON)
is in `_archive/FUTURE_IMPROVEMENTS_2026-05.md`; those provider databases were retired on Oct 6 in favour of
`providers_2027`.*

## 1. Must be done before any REAL client data (go-live blockers)
- [ ] **Move the engine to a BAA-covered host** (AWS or Azure) and the AI calls to Bedrock (engine name clean-up
      `normalize_drugs` + the n8n extract node). Railway + the direct Anthropic API are test-data only.
- [ ] **Lock the engine's endpoints.** Today anyone with the Railway URL can call `/process-soa`,
      `/client-comparison`, `/check-drugs`, `/plans-for-zip`. Add an access key (header) that only Roundabout and
      n8n hold; reject everything else; one key per agent install once apps call the engine directly.
- [ ] **2027 Star Ratings** loaded (README_REFRESH §5) — until then the client sheet shows 2026 stars.
- [ ] **Oct 15 CMS drug lists** loaded (README_REFRESH §6) and checked against Medicare.gov for 3–4 test clients.
- [ ] **Pharmacy names for 2027 networks** (NPI Registry lookup for pharmacies new in 2027 networks; today they
      carry over from 2026 names).
- [ ] **Release build** of Roundabout (full test suite) after Phases 31–33 are reviewed.
- [ ] Rotate the Azure secret that was pasted in chat earlier (if not done).
- [ ] Remove the old `.env` copy from the claude.ai project (holds keys) — see project cleanup note.

## 2. Known data gaps (and when they close)
| Gap | Effect on reports | Closes |
|---|---|---|
| Drug prices are 2026 prices carried to 2027 (option B) | Costs labelled "estimate" | Jan 20, 2027 quarterly file |
| Drugs new or newly generic in 2026 (e.g. generic sitagliptin) have no price | "no price on file — verify", cost left out | Jan 20, 2027 |
| Some carrier PDF rows couldn't be matched (e.g. generic Advair Diskus on several lists) | Can show "Not covered" wrongly | Oct 15 CMS lists (matched by drug ID, not name) |
| Medica + Humana 2027 drug lists not published | "Drug list not out yet", no cost | Oct 15 CMS file |
| Pharmacy networks carried from 2026 (`network_estimated`) | Nearby pharmacies may be off for 2027 | Oct 15 CMS file |
| Providers: only UHC (system level), Medica (2027 directory + system level) and agency confirmations | Other carriers "Not checked" | Weekly confirmations; carrier 2027 directories if Jill approves |
| ZIP→county uses Census 2020 ZCTAs | Rare edge ZIPs; street address lookup (Census geocoder) fixes most | — |

## 3. Next improvements (agreed or likely)
- [ ] App-only setup (no n8n/OneDrive per agent): engine extract endpoint on the BAA host; app works on a local
      folder (design in the master doc, Oct 2). Replaces per-agent n8n cloning.
- [ ] "Run again with different plans" (Completed Client Reports).
- [ ] Plans page (in-app Medicare.gov substitute) — after 2027 data is final.
- [ ] Provider "covered as another brand" note: e.g. Basaglar is on no MN list, but other insulin glargine brands are.
- [ ] Carrier-list second pass with the drug-name reader (reads ~70% of rows the carrier-list matcher missed) —
      only if a carrier list stays in use after Oct 15.
- [ ] Audit log replacement for the retired n8n SOA Tracker sheet (app-side log, no client health data).
- [ ] Code signing (Azure Artifact Signing) so Smart App Control doesn't block the exe.
- [ ] n8n cleanup after a few days of real use (export first): delete the old disconnected high-confidence chain,
      `Route by Confidence (8)`, `CIS Resume - After Review`, `CIS Engine - Report Back-Half`.
- [ ] Roundabout under version control (and point the update check at a real repo).

## 4. Later / ideas
- Expand beyond Minnesota (new service areas + data per state).
- GoodRx-style cash price comparison.
- Agent notification when reports are ready (Telegram / Teams).
