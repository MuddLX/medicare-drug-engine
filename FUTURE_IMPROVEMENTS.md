# Future Improvements & Known Gaps

## UHC/AARP Dental Network Coverage
**Status:** Low priority — investigate before Q4 2026 refresh

**Issue:** The UHC Medicare Advantage dental network is extremely sparse in our current DB — only 3 dental clinics found across all 13 Places Directory PDFs. This is because UHC's dental benefit for Medicare Advantage uses a supplemental dental network that is not well-represented in the myAARPMedicare.com Places Directory. The Places PDFs primarily list DME suppliers, dialysis centers, labs, and hospitals.

**Why it matters:** Agents looking up a client's dentist against UHC will almost always see "Not Found" even if that dentist is technically in-network. This could cause agents to steer clients away from UHC unnecessarily.

**Potential solutions to investigate:**
1. UHC's dental network for Medicare Advantage is administered through **Solera Dental** (formerly DentaQuest). Check if Solera publishes a Minnesota provider directory PDF or API.
2. Check `uhcprovider.com` for a separate dental network directory specific to UHC Medicare Advantage MN plans.
3. After the October 2026 MAPROVIDERS.JSON mandate kicks in, UHC must publish complete machine-readable provider data including dental — re-evaluate at that point.
4. Consider displaying "Dental benefit — verify at myAARPMedicare.com" for UHC dental providers rather than ✗ Not Found, to avoid false negatives.

---

## October 2026 — MAPROVIDERS.JSON Mandate
**Status:** Calendar reminder — revisit October 2026

CMS's machine-readable provider directory mandate (CMS-4208-F2) takes effect for contract year 2026. Starting October 2026, all Medicare Advantage organizations including UHC and Allina Health Aetna must publish complete, machine-readable provider directories in a standardized format.

This will enable:
- Full UHC in-network provider database (currently 7,717 providers from PDF parsing)
- Full Allina Health Aetna H3219 provider database (currently no provider data at all)
- Automated quarterly refresh of provider databases alongside drug data refresh

**Action:** In October 2026 during the annual plan data refresh, check `medicare.gov/plan-compare` and CMS data releases for MAPROVIDERS.JSON files for H2001 (UHC) and H3219 (Aetna).

---

## Allina Health Aetna H3219 Provider Directory
**Status:** Blocked until October 2026 MAPROVIDERS.JSON mandate

No publicly accessible provider directory exists for Allina Health Aetna H3219 in machine-readable format. Their directory is online-only at `AllinaHealthAetnaMedicare.com/findprovider`. The FHIR API is partially implemented but incomplete.

Current behavior: Drug costs display correctly for Aetna. Provider column shows "Verify at AllinaHealthAetnaMedicare.com".

---

## General Future Enhancements
- [ ] n8n migration for instant Google Drive trigger (planned September 2026)
- [ ] OOP max ($2,100 in 2026) added to plan comparison
- [ ] Expand to other states (requires new plan config + DB rebuild per state)
- [ ] Telegram bot notification for agent when report is ready
- [ ] Web portal — React frontend for direct data entry without PDF
- [ ] GoodRx API integration for retail cash price comparison
- [ ] Dedicated "Agent Notes" section on SOA form for cleaner custom plan requests
