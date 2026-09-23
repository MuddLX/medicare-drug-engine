"""
BCBS Provider Directory Parser v1
Builds bcbs_providers.db from three BCBS MN Medicare Advantage PDFs:
  - Metro/West provider directory (pre-extracted text file)
  - South provider directory (pre-extracted text file)
  - Dental directory (actual PDF, pypdf extraction)

Output: bcbs_providers.db  — upload to R2 bucket 'medicare-db'

Run locally:
  python build_bcbs_db.py

Requires:
  pip install pypdf
"""

import re
import os
import sqlite3
import pypdf

# ── Input paths ────────────────────────────────────────────────────────────────
MEDICAL_DIRS = [
    r"BC BS 2026 Provider PDFs\F10957R08_2026_MED_ADV_Metro-West_May_Print_tagged - 2026-medicare-advantage-metro-west-provider-directory.pdf",
    r"BC BS 2026 Provider PDFs\F11007R07_2026_MAPD_SOUTHERN_May_Print_tagged - 2026-medicare-advantage-south-provider-directory.pdf",
]
DENTAL_PDF = r"BC BS 2026 Provider PDFs\F11141R06_2026_Minnesota_Dental_Directory_May_tagged - 2026-minnesota-medicare-dental-directory.pdf"
OUTPUT_DB  = "bcbs_providers.db"

BATCH_SIZE = 500

# ── Dental credential tokens ───────────────────────────────────────────────────
DENTAL_CREDS = {
    'DDS', 'DMD', 'DDS,', 'DMD,',
}

# ── Dental specialty labels (exact strings that appear as section headers) ─────
DENTAL_SPECIALTIES = {
    'General Dentist', 'Oral Surgery', 'Endodontist', 'Periodontist',
    'Prosthodontist', 'Pediatric Dentist', 'Multi-Specialty', 'Orthodontist',
    'Oral Surgeon', 'Oral and Maxillofacial Surgery',
}

# ── Medical specialty labels (from section headers in medical directories) ─────
# These match what appears verbatim in the provider listings
MEDICAL_SPECIALTIES = {
    'Family Practice', 'Internal Medicine', 'General Practice', 'Pediatrics',
    'Obstetrics', 'Gynecology', 'OB/GYN', 'Allergy/Immunology',
    'Allergy and Immunology', 'Cardiology', 'Dermatology', 'Endocrinology',
    'Gastroenterology', 'Hematology', 'Nephrology', 'Neurology', 'Oncology',
    'Ophthalmology', 'Orthopedics', 'Otolaryngology', 'Podiatry', 'Psychiatry',
    'Pulmonology', 'Radiology', 'Rheumatology', 'Urology', 'Surgery',
    'Physical Therapy', 'Occupational Therapy', 'Speech Language Pathology',
    'Audiology', 'Optometry', 'Chiropractic', 'Pain Management',
    'Sleep Medicine', 'Infectious Disease', 'Geriatrics',
    'Ear Nose & Throat', 'Vascular Surgery', 'Orthopedic Surgery',
    'General Surgery', 'Colorectal Surgery', 'Thoracic Surgery',
    'Plastic Surgery', 'Oral Surgery', 'Neonatology', 'Behavioral Health',
    'Psychology', 'Neuropsychology', 'Hospitalist', 'Sports Medicine',
    'Geriatric Psychiatry', 'Hematology Oncology', 'Medical Oncology',
    'Oncology/Hematology', 'Mental Health', 'Renal Dialysis',
    'Home Health Care', 'Skilled Nursing', 'Urgent Care',
    'Hospice', 'Wound Care', 'Addiction Medicine', 'Reproductive',
    'Fertility', 'Maternal Fetal', 'OBGYN',
}

# ── Section headers in medical directories (mark start of provider type) ──────
MEDICAL_SECTION_HEADERS = {
    'Primary Care Clinics', 'Hospitals', 'Home Health Care Providers',
    'Skilled Nursing Facilities', 'Outpatient Rehabilitation Centers',
    'Ambulatory Surgical Centers', 'Urgent Care Centers',
    'Allergy/Immunology', 'Audiology', 'Cardiology', 'Chiropractors',
    'Dermatology', 'Endocrinology', 'Eye/Vision Care/Discount Eyewear',
    'Eye/Vision Care/Ophthalmology', 'Eye/Vision Care/Optometry',
    'Gastroenterology', 'Hearing Aids/Routine Hearing Exams (TruHearing)',
    'Medical Suppliers', 'Mental Health/Inpatient', 'Mental Health/Outpatient',
    'Nephrology', 'Neurology', 'OB/GYN', 'Oncology/Hematology',
    'Oral Surgeons', 'Orthopedics', 'Otolaryngology', 'Peer Specialists',
    'Podiatry', 'Pulmonology', 'Renal Dialysis', 'Rheumatology', 'Surgery',
    'Therapy/Occupational Therapists', 'Therapy /Physical Therapists',
    'Therapy/Speech Therapists', 'Urology',
}

# ── Regexes ────────────────────────────────────────────────────────────────────
PHONE_RE      = re.compile(r'^\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}')
ZIP_RE        = re.compile(r'\d{5}')
CITY_STATE_RE = re.compile(r'^[A-Za-z][A-Za-z\s\-]+,\s*(MN|IA|ND|SD|WI)\s+\d{5}')
# Medical city marker: line starts with 'x' followed by a city name
CITY_MARKER_RE = re.compile(r'^x([A-Za-z][A-Za-z\s\-]+),\s*(MN|IA|ND|SD|WI)$')
ADDRESS_RE    = re.compile(r'^\d+\s+\w')
# Dental: "CITY NAME - 55124" or "CITY NAME 55124"
DENTAL_CITY_RE = re.compile(r'^([A-Z][A-Z\s\-\.]+?)\s*[-–]?\s*(\d{5})(?:\s|$)')
# Dental provider name: "LAST, FIRST M. , DDS" or "LAST LAST , FIRST , DDS"
DENTAL_PROVIDER_RE = re.compile(
    r'^([A-Z][A-Z\s\-\'\.]+?),\s*(.+?)\s*,\s*(DDS|DMD)\s*$',
    re.IGNORECASE
)
# Or clinic name (all caps, no credential at end)
# Dental not-accepting marker
NOT_ACCEPTING_RE = re.compile(r'\[Currently not accepting', re.IGNORECASE)
PAGE_FOOTER_RE   = re.compile(r'Minnesota Medicare Dental Network Page \d+')


# ── DB setup ───────────────────────────────────────────────────────────────────
def init_db(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE providers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,   -- 'medical' or 'dental'
            last_name   TEXT,
            first_name  TEXT,
            credentials TEXT,
            clinic_name TEXT,
            address     TEXT,
            city        TEXT,
            state       TEXT,
            zip         TEXT,
            specialty   TEXT,
            section     TEXT,
            accepting   TEXT DEFAULT 'Y'
        )
    """)
    for col in ('last_name', 'city', 'state', 'zip', 'specialty', 'source'):
        conn.execute(f"CREATE INDEX idx_bcbs_{col} ON providers({col} COLLATE NOCASE)")
    conn.commit()
    return conn


def flush(conn, batch):
    if not batch:
        return
    conn.executemany("""
        INSERT INTO providers
          (source,last_name,first_name,credentials,clinic_name,
           address,city,state,zip,specialty,section,accepting)
        VALUES
          (:source,:last_name,:first_name,:credentials,:clinic_name,
           :address,:city,:state,:zip,:specialty,:section,:accepting)
    """, batch)
    conn.commit()
    batch.clear()


# ══════════════════════════════════════════════════════════════════════════════
# MEDICAL DIRECTORY PARSER
# Structure (pre-extracted text files, clinic-centric):
#   Section header  → "Primary Care Clinics", "Cardiology", etc.
#   State header    → "Minnesota", "Iowa", "Wisconsin"
#   County          → title-case word, not a known specialty/city
#   City marker     → "xBurnsville, MN"  (leading lowercase x)
#   Clinic name     → one or two lines, may be all-caps or title-case
#   Address         → starts with digit
#   City/state/zip  → "Burnsville, MN 55337"
#   Phone           → "(952) 435-..."
#   Specialty       → known specialty string (may repeat)
#   Languages       → "Languages Spoken:" then language list
#   Accepting       → "Accepting New Patients"
# ══════════════════════════════════════════════════════════════════════════════

def parse_medical_file(path, conn, batch, total):
    source_name = "Metro/West" if "Metro" in path else "South"
    print(f"\n  Parsing medical: {os.path.basename(path)}")

    # Detect whether file is a real PDF or pre-extracted text
    with open(path, 'rb') as f:
        header = f.read(5)
    is_pdf = (header == b'%PDF-')

    if is_pdf:
        import warnings
        warnings.filterwarnings('ignore')
        reader = pypdf.PdfReader(path)
        print(f"    Pages: {len(reader.pages)}")
        all_text = []
        for page in reader.pages:
            text = page.extract_text() or ''
            all_text.append(text)
        raw_content = '\n'.join(all_text)
        lines = [l.rstrip('\r\n') for l in raw_content.split('\n')]
    else:
        # Pre-extracted text file (project/testing copies)
        with open(path, 'r', errors='replace') as f:
            lines = [l.rstrip('\r\n') for l in f.readlines()]

    # State ── county ── city ── clinic ── address ── city/state/zip ── phone
    # ── specialties ── accepting
    state       = ''
    county      = ''
    city        = ''
    state_abbr  = ''
    section     = 'Primary Care'
    specialty   = ''
    in_lang     = False
    in_header   = True  # skip front-matter until "Primary Care Clinics"
    seen_city   = False  # True after we've parsed a city marker (x-prefix line)

    # Current clinic context
    clinic_lines  = []
    address       = ''
    clinic_zip    = ''
    clinic_city   = ''
    clinic_state  = ''
    specialties   = []    # all specialties seen for current clinic
    accepting     = 'Y'
    clinic_name   = ''

    def save_clinic():
        nonlocal total
        if not clinic_city and not clinic_name:
            return
        sp = ', '.join(specialties) if specialties else specialty
        rec = dict(
            source=source_name,
            last_name=None,
            first_name=None,
            credentials=None,
            clinic_name=clinic_name or ' '.join(clinic_lines).strip(),
            address=address,
            city=clinic_city or city,
            state=clinic_state or state_abbr or state[:2],
            zip=clinic_zip,
            specialty=sp or section,
            section=section,
            accepting=accepting,
        )
        batch.append(rec)
        total[0] += 1
        if len(batch) >= BATCH_SIZE:
            flush(conn, batch)

    def reset_clinic():
        nonlocal clinic_lines, address, clinic_zip, clinic_city
        nonlocal clinic_state, specialties, accepting, clinic_name
        clinic_lines  = []
        address       = ''
        clinic_zip    = ''
        clinic_city   = ''
        clinic_state  = ''
        specialties   = []
        accepting     = 'Y'
        clinic_name   = ''

    i = 0
    while i < len(lines):
        raw  = lines[i]
        t    = raw.strip()
        i   += 1

        if not t:
            continue

        # Skip pages that are mostly garbled (non-printable)
        printable = sum(1 for c in t if c.isprintable() and ord(c) < 128)
        if len(t) > 5 and printable / len(t) < 0.85:
            continue

        # ── Wait for first section header before processing ────────────────
        if in_header:
            if t in MEDICAL_SECTION_HEADERS or t == 'Primary Care Clinics':
                in_header = False
                section   = t
            continue

        # ── Page number lines (single digit or roman numeral) ──────────────
        if re.match(r'^(I{1,4}V?|V?I{0,3}|\d{1,4})$', t):
            continue

        # ── Language block ─────────────────────────────────────────────────
        if t.startswith('Languages Spoken'):
            in_lang = True
            continue
        if in_lang:
            # Language lines are comma-separated language names, may be
            # split mid-word. End when we hit something else.
            if re.match(r'^[A-Za-z,\s]+$', t) and not t in MEDICAL_SPECIALTIES \
                    and not t in MEDICAL_SECTION_HEADERS:
                continue  # still in language list
            else:
                in_lang = False
                # fall through to process t

        # ── Section header ─────────────────────────────────────────────────
        if t in MEDICAL_SECTION_HEADERS:
            save_clinic()
            reset_clinic()
            section   = t
            specialty = ''
            seen_city = False
            continue

        # ── State header ───────────────────────────────────────────────────
        if t in ('Minnesota', 'Iowa', 'Wisconsin', 'North Dakota', 'South Dakota'):
            save_clinic()
            reset_clinic()
            state_map  = {'Minnesota': 'MN', 'Iowa': 'IA', 'Wisconsin': 'WI',
                          'North Dakota': 'ND', 'South Dakota': 'SD'}
            state      = t
            state_abbr = state_map.get(t, t[:2])
            city       = ''
            county     = ''
            seen_city  = False
            continue

        # ── County (title-case, 1-3 words, not a known specialty) ──────────
        # Counties appear after a state header and before a city marker
        # They are title-case, no digits, not a specialty
        # CRITICAL: once we've seen a city marker (seen_city=True), title-case
        # lines are clinic names, not counties.
        if (not seen_city
                and not ADDRESS_RE.match(t)
                and not PHONE_RE.match(t)
                and not CITY_MARKER_RE.match(t)
                and not CITY_STATE_RE.match(t)
                and t not in MEDICAL_SPECIALTIES
                and t not in MEDICAL_SECTION_HEADERS
                and re.match(r'^[A-Z][a-z]', t)
                and len(t.split()) <= 4
                and not re.search(r'\d', t)
                and not t.startswith('Accepting')
                and not t.startswith('Languages')):
            county = t
            continue

        # ── City marker: "xBurnsville, MN" ────────────────────────────────
        m = CITY_MARKER_RE.match(t)
        if m:
            save_clinic()
            reset_clinic()
            city       = m.group(1).strip()
            state_abbr = m.group(2)
            seen_city  = True
            continue

        # ── Accepting new patients ─────────────────────────────────────────
        if t == 'Accepting New Patients':
            accepting = 'Y'
            continue
        if 'Not Accepting' in t or 'not accepting' in t.lower():
            accepting = 'N'
            continue

        # ── Specialty ─────────────────────────────────────────────────────
        if t in MEDICAL_SPECIALTIES:
            specialties.append(t)
            specialty = t
            continue

        # ── Phone ──────────────────────────────────────────────────────────
        if PHONE_RE.match(t):
            continue

        # ── City/State/Zip on one line: "Burnsville, MN 55337" ────────────
        m = CITY_STATE_RE.match(t)
        if m:
            # parse city, state, zip
            parts = t.rsplit(',', 1)
            if len(parts) == 2:
                clinic_city  = parts[0].strip()
                rest         = parts[1].strip()
                tokens       = rest.split()
                if len(tokens) >= 2:
                    clinic_state = tokens[0]
                    clinic_zip   = tokens[1]
                elif len(tokens) == 1:
                    # might be just state
                    clinic_state = tokens[0]
            continue

        # ── Address (starts with digit) ────────────────────────────────────
        if ADDRESS_RE.match(t):
            if address:
                # Second address line (e.g. "Ste 100") — check if it's a continuation
                if re.match(r'^(Ste|Suite|#|Floor|Bldg)', t, re.I):
                    address += ', ' + t
                else:
                    # New clinic starting without explicit city marker
                    save_clinic()
                    reset_clinic()
                    address = t
            else:
                address = t
            continue

        # ── Address continuation (suite line starting with digit) ──────────
        if re.match(r'^(Ste|Suite|#|Floor|Bldg|\d+)', t, re.I) and address \
                and not clinic_city:
            address += ', ' + t
            continue

        # ── Clinic name (anything else that looks like a name) ─────────────
        # Could be all-caps or title-case clinic name.
        # Skip if line exactly matches the current city (PDF repeats city under name).
        if t and not re.search(r'^[\\\x00-\x1f]', t) and t != city:
            clinic_lines.append(t)
            clinic_name = ' '.join(clinic_lines).strip()

    # Save last clinic
    save_clinic()
    flush(conn, batch)
    return total[0]


# ══════════════════════════════════════════════════════════════════════════════
# DENTAL DIRECTORY PARSER
# Structure (actual PDF via pypdf, provider-centric):
#   MINNESOTA              ← state header (page 6+ only)
#   CITY NAME 55124        ← ALLCAPS city + zip (also "CITY - 55124")
#   General Dentist        ← specialty section
#   LAST, FIRST M. , DDS   ← provider name
#   14929 FLORENCE TRL...  ← address (ALLCAPS street)
#   (952) 432-7366         ← phone
#   Language(s): AR, FR    ← optional
#   [Currently not acc...  ← not accepting (appears BEFORE the name it applies to)
#
# Complications:
#   - Multi-column merging: two providers on one line
#   - Clinic names (all-caps, no DDS/DMD)
#   - "[" bracket appears as not-accepting flag mid-line
#   - Page headers repeat "Medicare Dental Network / Directory..." etc.
# ══════════════════════════════════════════════════════════════════════════════

def split_dental_merged_line(line):
    """
    Split merged multi-column dental lines.
    Handles 2 and 3 column merges like:
      "LAST, FIRST , DDS  LAST2, FIRST2 , DDS"
      "CLINIC MOUA, CHIA W. , DDS TRAN, BAO Q. , DDS"
    Returns list of 1, 2, or 3 strings.
    """
    parts = []
    remaining = line

    # Repeatedly find the next provider name after a DDS/DMD credential
    while remaining:
        m = re.search(r'\s(DDS|DMD)\s+([A-Z][A-Z\s\-\']+,)', remaining)
        if m:
            split_at = m.start(2)
            parts.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        else:
            parts.append(remaining.strip())
            break

    if len(parts) > 1:
        return parts

    # Fallback: split on double-address pattern
    m = re.search(r'(\(\d{3}\)\s*\d{3}-\d{4})\s+(\d)', line)
    if m:
        split_at = m.end(1)
        return [line[:split_at].strip(), line[split_at:].strip()]

    return [line]


def parse_dental_name(t):
    """
    Parse dental provider name line.
    Formats:
      "SMITH, JOHN A. , DDS"
      "SMITH JONES , MARY B. , DDS"
      "CLINIC NAME (all caps, no DDS)"
    Returns (last_name, first_name, credentials, is_provider, accepting_flag)
    accepting_flag: True = not accepting (has '[' before name)
    """
    not_accepting = '[' in t
    t_clean = t.replace('[', '').replace(']', '').strip()

    # Try provider pattern: ends with DDS or DMD
    m = re.match(
        r'^([A-Z][A-Z\s\-\'\.]+?)\s*,\s*(.+?)\s*,\s*(DDS|DMD)\s*$',
        t_clean, re.IGNORECASE
    )
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), True, not_accepting

    # Try without middle name comma: "SMITH , DDS" (just last name)
    m = re.match(r'^([A-Z][A-Z\s\-\'\.]+?)\s*,\s*(DDS|DMD)\s*$', t_clean, re.IGNORECASE)
    if m:
        return m.group(1).strip(), '', m.group(2).upper(), True, not_accepting

    # Clinic name (all-caps, no DDS)
    if re.match(r'^[A-Z0-9][A-Z0-9\s\-&/\.,\']+$', t_clean) and len(t_clean) > 3:
        return None, None, None, False, not_accepting

    return None, None, None, False, not_accepting


def parse_dental_pdf(path, conn, batch, total):
    print(f"\n  Parsing dental: {os.path.basename(path)}")

    reader = pypdf.PdfReader(path)
    pages  = len(reader.pages)
    print(f"    Pages: {pages}")

    city      = ''
    state_str = 'MN'
    zip_str   = ''
    specialty = 'General Dentist'
    address   = ''
    cur_name  = None   # (last, first, creds, accepting)
    clinic_nm = ''

    # Page header lines to skip
    SKIP_LINES = {
        'Medicare Dental Networ k',
        'Medicare Dental Network',
        'Directory of Participating Dental Providers',
        'Minnesota',
        'MINNESOTA',
        'County',
    }

    def save_provider(last, first, creds, acc, addr, cty, st, zp, spec, clinic):
        nonlocal total
        if not last and not clinic:
            return
        batch.append(dict(
            source='dental',
            last_name=last,
            first_name=first,
            credentials=creds,
            clinic_name=clinic,
            address=addr,
            city=cty,
            state=st,
            zip=zp,
            specialty=spec,
            section='Dental',
            accepting='N' if not acc else 'Y',
        ))
        total[0] += 1
        if len(batch) >= BATCH_SIZE:
            flush(conn, batch)

    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ''
        raw_lines = text.split('\n')

        # Split any merged lines
        lines = []
        for raw in raw_lines:
            parts = split_dental_merged_line(raw.strip())
            lines.extend(parts)

        pending_not_accepting = False

        for raw in lines:
            t = raw.strip()
            if not t:
                continue

            # Skip page footers
            if PAGE_FOOTER_RE.match(t):
                continue
            if t.startswith('This list of dental providers'):
                continue
            if t.startswith('Proprietary Confidential'):
                continue
            if t in SKIP_LINES:
                continue

            # Not-accepting flag (standalone line)
            if NOT_ACCEPTING_RE.match(t):
                pending_not_accepting = True
                continue

            # Language line
            if t.startswith('Language(s):'):
                continue

            # Page continuation header: "CITY - 55xxx (continued)"
            continued = t.endswith('(continued)')
            if continued:
                t = t.replace('(continued)', '').strip()

            # Specialty header
            if t in DENTAL_SPECIALTIES:
                specialty = t
                continue

            # City/Zip header: "APPLE VALLEY 55124" or "APPLE VALLEY - 55124"
            m = DENTAL_CITY_RE.match(t)
            if m and not re.search(r'\b(DDS|DMD)\b', t, re.I):
                city    = m.group(1).strip().title()
                zip_str = m.group(2)
                # state stays MN (this is a MN dental directory)
                address = ''
                cur_name = None
                continue

            # Phone line
            if PHONE_RE.match(t):
                # If we have a pending provider name, save it now
                # (phone comes after address)
                continue

            # Address line (all-caps street address)
            if ADDRESS_RE.match(t) and t == t.upper().replace(t.upper()[0], t[0], 1) or ADDRESS_RE.match(t):
                if ADDRESS_RE.match(t):
                    address = t
                    continue

            # Provider or clinic name
            # Check for DDS/DMD credential
            has_cred = bool(re.search(r'\b(DDS|DMD)\b', t, re.I))
            not_acc_in_line = '[' in t

            if has_cred or (re.match(r'^[A-Z][A-Z\s\-\']+,', t) and len(t) > 5):
                # Try to parse as provider
                last, first, creds, is_prov, not_acc_flag = parse_dental_name(t)

                if is_prov:
                    # Save previous
                    if cur_name:
                        save_provider(
                            cur_name[0], cur_name[1], cur_name[2],
                            cur_name[3], address, city, state_str,
                            zip_str, specialty, clinic_nm
                        )
                        clinic_nm = ''

                    accepting = not (not_acc_flag or pending_not_accepting)
                    cur_name  = (last, first, creds, accepting)
                    address   = ''
                    pending_not_accepting = False

                else:
                    # Clinic name
                    if cur_name:
                        save_provider(
                            cur_name[0], cur_name[1], cur_name[2],
                            cur_name[3], address, city, state_str,
                            zip_str, specialty, clinic_nm
                        )
                        cur_name  = None
                        address   = ''
                    t_clean = t.replace('[', '').replace(']', '').strip()
                    clinic_nm = t_clean

            elif re.match(r'^[A-Z][A-Z0-9\s\-&/\.,\']+$', t) and len(t) > 4:
                # All-caps: could be clinic name or address
                if ADDRESS_RE.match(t):
                    address = t
                else:
                    if cur_name:
                        save_provider(
                            cur_name[0], cur_name[1], cur_name[2],
                            cur_name[3], address, city, state_str,
                            zip_str, specialty, clinic_nm
                        )
                        cur_name  = None
                        address   = ''
                    t_clean = t.replace('[', '').replace(']', '').strip()
                    clinic_nm = t_clean

        # End of page — save any pending provider
        if cur_name:
            save_provider(
                cur_name[0], cur_name[1], cur_name[2],
                cur_name[3], address, city, state_str,
                zip_str, specialty, clinic_nm
            )
            cur_name  = None
            clinic_nm = ''
            address   = ''

    flush(conn, batch)
    return total[0]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=== BCBS Provider Directory Parser ===\n")

    conn  = init_db(OUTPUT_DB)
    batch = []
    total = [0]

    # ── Medical directories ────────────────────────────────────────────────
    seen_clinics = set()  # deduplicate across Metro/West + South
    for path in MEDICAL_DIRS:
        if not os.path.exists(path):
            print(f"  WARNING: Not found: {path}")
            continue
        before = total[0]
        parse_medical_file(path, conn, batch, total)
        print(f"    Loaded {total[0] - before:,} medical providers")

    # ── Dental directory ───────────────────────────────────────────────────
    if os.path.exists(DENTAL_PDF):
        before = total[0]
        parse_dental_pdf(DENTAL_PDF, conn, batch, total)
        print(f"\n    Loaded {total[0] - before:,} dental providers")
    else:
        print(f"\n  WARNING: Dental PDF not found: {DENTAL_PDF}")

    flush(conn, batch)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"TOTAL: {total[0]:,} providers")

    cur = conn.cursor()
    print("\nBy source:")
    for row in cur.execute("SELECT source, COUNT(*) FROM providers GROUP BY source"):
        print(f"  {row[0]}: {row[1]:,}")

    print("\nMedical - top specialties:")
    for row in cur.execute("""
        SELECT specialty, COUNT(*) as n FROM providers
        WHERE source != 'dental'
        GROUP BY specialty ORDER BY n DESC LIMIT 10
    """):
        print(f"  {row[0]}: {row[1]:,}")

    print("\nDental - top specialties:")
    for row in cur.execute("""
        SELECT specialty, COUNT(*) as n FROM providers
        WHERE source = 'dental'
        GROUP BY specialty ORDER BY n DESC LIMIT 8
    """):
        print(f"  {row[0]}: {row[1]:,}")

    print("\nTop cities (medical):")
    for row in cur.execute("""
        SELECT city, COUNT(*) as n FROM providers
        WHERE source != 'dental' AND city != ''
        GROUP BY city ORDER BY n DESC LIMIT 10
    """):
        print(f"  {row[0]}: {row[1]:,}")

    print("\nSample dental - Apple Valley DDS:")
    for row in cur.execute("""
        SELECT last_name, first_name, credentials, specialty, city, accepting
        FROM providers WHERE source='dental' AND city LIKE '%Apple Valley%'
        LIMIT 5
    """):
        print(f"  {row[0]}, {row[1]} {row[2]} | {row[3]} | {row[4]} | acc:{row[5]}")

    print("\nSample medical - Hennepin cardiology:")
    for row in cur.execute("""
        SELECT clinic_name, city, specialty, accepting
        FROM providers
        WHERE source != 'dental'
          AND specialty LIKE '%Cardiology%'
          AND city IN ('Minneapolis','Edina','Bloomington','St. Louis Park')
        LIMIT 5
    """):
        print(f"  {row[0][:40]} | {row[1]} | {row[2]} | acc:{row[3]}")

    print(f"\nMissing city: {cur.execute('SELECT COUNT(*) FROM providers WHERE city=?',('',)).fetchone()[0]}")
    print(f"Not accepting: {cur.execute('SELECT COUNT(*) FROM providers WHERE accepting=?',('N',)).fetchone()[0]}")
    db_size = os.path.getsize(OUTPUT_DB) / 1024 / 1024
    print(f"DB size: {db_size:.1f} MB")
    conn.close()
    print(f"\nOutput: {OUTPUT_DB}")
    print("Next: upload to R2 bucket 'medicare-db' as 'bcbs_providers.db'")


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--preview':
        # Preview mode: dump first 100 clean lines from each medical PDF
        # so you can verify structure before running full parse
        import warnings, pypdf as _pypdf
        warnings.filterwarnings('ignore')
        for path in MEDICAL_DIRS:
            if not os.path.exists(path):
                print(f"Not found: {path}")
                continue
            print(f"\n=== PREVIEW: {os.path.basename(path)} ===")
            with open(path, 'rb') as f:
                hdr = f.read(5)
            if hdr == b'%PDF-':
                reader = _pypdf.PdfReader(path)
                text = ''
                for pg in reader.pages[:8]:
                    text += (pg.extract_text() or '') + '\n'
                lines = [l.strip() for l in text.split('\n') if l.strip()]
            else:
                with open(path, 'r', errors='replace') as f:
                    lines = [l.strip() for l in f if l.strip()]
            # Print first 100 clean lines
            count = 0
            for line in lines:
                printable = sum(1 for c in line if c.isprintable() and ord(c) < 128)
                if len(line) > 3 and printable / len(line) > 0.9:
                    print(f"  {repr(line)}")
                    count += 1
                    if count >= 100:
                        break
    elif len(sys.argv) > 1 and sys.argv[1] == '--preview-dental':
        import warnings, pypdf as _pypdf
        warnings.filterwarnings('ignore')
        if not os.path.exists(DENTAL_PDF):
            print(f"Not found: {DENTAL_PDF}")
        else:
            print(f"\n=== DENTAL PREVIEW: {os.path.basename(DENTAL_PDF)} ===")
            reader = _pypdf.PdfReader(DENTAL_PDF)
            print(f"Pages: {len(reader.pages)}")
            # Show pages 5-8 where data starts
            for pg_num in range(4, min(9, len(reader.pages))):
                print(f"\n--- Page {pg_num+1} ---")
                text = reader.pages[pg_num].extract_text() or ''
                for line in text.split('\n')[:40]:
                    if line.strip():
                        print(f"  {repr(line.strip())}")
    else:
        main()
