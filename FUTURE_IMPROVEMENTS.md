# Open items, known gaps & future improvements
*Updated October 8, 2026 (evening). The May 2026 list (old carrier provider directories, UHC dental, MAPROVIDERS.JSON)
is in `_archive/FUTURE_IMPROVEMENTS_2026-05.md`; those provider databases were retired on Oct 6 in favour of
`providers_2027`.*

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
- [ ] **Oct 15 CMS drug lists + pharmacy networks** loaded (README_REFRESH §6) and checked against Medicare.gov for
      3–4 fake test clients. Turns on "Not covered" and pharmacy names automatically.
- [ ] **Pharmacy names for 2027 networks** (NPI Registry lookup for pharmacies new in 2027).
- [ ] Roundabout **Phase 43** (delete any sheet from In Progress) + version bump to 0.2.0 + release build.
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
