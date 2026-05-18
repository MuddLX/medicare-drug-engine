"""
build_hp_db.py
Parses the HealthPartners Journey 2026 Metro & Central MN Provider Directory PDF
into a SQLite database and uploads it to Cloudflare R2.

PDF sections handled:
  - Medical providers (pages 6–574):  clinic-centric
  - Dental providers  (pages 773–892): clinic-centric

Output: hp_providers.db
Run from: C:\\Users\\Mudd\\medicare_drug_engine\\
"""

import re
import sqlite3
import os
import sys

from pypdf import PdfReader
import boto3
from botocore.client import Config

# ── CONFIG ───────────────────────────────────────────────────────────────────
PDF_PATH   = r"Health Partners Provider PDFs\HealthPartners Journey Metro and Central Minnesota Provider Directory - directory-journey-provider-directory.pdf"
DB_PATH    = "hp_providers.db"
R2_BUCKET  = "medicare-db"
R2_KEY     = "hp_providers.db"

MEDICAL_START = 6
MEDICAL_END   = 574
DENTAL_START  = 773
DENTAL_END    = 892

# ── REGEX PATTERNS ────────────────────────────────────────────────────────────

ADDRESS_RE = re.compile(
    r"^\d+\s+.+?\b(Ave|St|Rd|Blvd|Dr|Ln|Way|Pkwy|Pl|Ct|Cir|Hwy|Loop|Trl|Sq|"
    r"Row|Path|Xing|Ter|Blf|NW|NE|SW|SE)\b",
    re.IGNORECASE
)

CITY_LINE_RE = re.compile(r"^(.+?),\s*MN\s+(\d{5})", re.IGNORECASE)

PHONE_RE = re.compile(r"^\(\d{3}\)\s*\d{3}-\d{4}")

CREDENTIAL_RE = re.compile(
    r",\s*(MD|DO|PA[-\s]?C?|NP[-\s]?[A-Z]*|DNP[-\s]?[A-Z]*|CNM|CNS[-\s]?[A-Z]*|"
    r"MBBS|DDS|DMD|DPM|OT(RL)?|PT(,\s*DPT)?|DPT|SLP|CCC[-\s]?SLP|CCC[-\s]?A|"
    r"AuD|RD,?\s*LD?|ADT|AGPC|NP-F|NP-A|FAAD|FACS|PharmD|DC|OD|PhD|MS,?\s*OT|"
    r"MOT|OTRL|BDS|L\.Ac\.?|LAc|LMFT|LICSW|LPCC|LPC|LADC)\b",
    re.IGNORECASE
)

DISCARD_RE = re.compile(
    r"^(ADA accessible|Cultural competency|Cultural capabilities|Languages Spoken$|"
    r"Hospital$|PHYSICIANS$|To get the most up-to-date|healthpartners\.com|"
    r"PRIMARY CARE PROVIDERS|AND SPECIALISTS|DENTAL PROVIDERS|"
    r"Hospitals and Outpatient|Skilled Nursing|Outpatient Mental|Online Clinic|"
    r"Hearing Aids|Urgent Care|Walk-In Clinics|Dental Providers$|"
    r"Providers are organized|Minnesota$|Section\s+[I\d])",
    re.IGNORECASE
)

METADATA_VALUE_RE = re.compile(
    r"^(Yes$|No$|Accepting New Patients|completed:)",
    re.IGNORECASE
)

SPECIALTY_TERMS_RE = re.compile(
    r"\b(Medicine|Surgery|Therapy|Pathology|Care|Dentistry|Endodontics|"
    r"Periodontics|Orthodontics|Pediatrics|Gynecology|Cardiology|Neurology|"
    r"Oncology|Ophthalmology|Dermatology|Urology|Orthopedics|Radiology|"
    r"Rheumatology|Immunology|Audiology|Nutrition|Podiatry|Psychiatry|"
    r"Anesthesiology|Gastroenterology|Pulmonology|Pulmonary|Nephrology|"
    r"Endocrinology|Allergy|Hematology|Geriatrics|Hospitalist|Internal|"
    r"Maxillofacial|Prosthodontics|Implantology|Podiatric|Obstetrics|"
    r"Orthopedic|Chiropractic|Acupuncture|Dialysis|Rehabilitation|"
    r"Neonatology|Otolaryngology|Spine|Sports|Wound|Infectious|"
    r"Vascular|Thoracic|Plastic|Colorectal|Bariatric|Transplant|"
    r"Palliative|Hospice|Oral|Adult Medicine|Pain|Foot and Ankle|"
    r"Hand|Critical|Preventive)\b",
    re.IGNORECASE
)


def is_specialty_header(line):
    if CREDENTIAL_RE.search(line): return False
    if ADDRESS_RE.match(line): return False
    if CITY_LINE_RE.match(line): return False
    if PHONE_RE.match(line): return False
    if len(line.split()) > 7: return False
    return bool(SPECIALTY_TERMS_RE.search(line))


def clean_lines(text):
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line: continue
        if re.match(r"^\d+$", line): continue
        if DISCARD_RE.match(line): continue
        if METADATA_VALUE_RE.match(line): continue
        out.append(line)
    return out


def parse_name(full_name):
    m = CREDENTIAL_RE.search(full_name)
    if not m:
        return None
    name_part   = full_name[:m.start()].strip().rstrip(",")
    credentials = full_name[m.start():].lstrip(",").strip()
    parts = name_part.split()
    if not parts:
        return None
    return " ".join(parts[:-1]), parts[-1], credentials


def extract_text_pages(pdf_path, start_page, end_page):
    reader = PdfReader(pdf_path)
    end_page = min(end_page, len(reader.pages))
    return [(i + 1, reader.pages[i].extract_text() or "")
            for i in range(start_page - 1, end_page)]


def parse_section(pages_text, source_label):
    """
    Address-anchored strategy:
    1. Flatten all pages into one line list
    2. Scan for 'City, MN ZIP' anchors
    3. For each anchor, look back to find address and clinic name
    4. Collect provider names (by credential detection) between clinic blocks
    """
    records = []

    # Flatten
    all_lines = []
    for _, text in pages_text:
        all_lines.extend(clean_lines(text))

    n = len(all_lines)

    # Find all city-line positions
    city_indices = []
    for i, line in enumerate(all_lines):
        if CITY_LINE_RE.match(line):
            city_indices.append(i)

    # Build clinic blocks
    clinic_blocks = []
    for ci, city_idx in enumerate(city_indices):
        m = CITY_LINE_RE.match(all_lines[city_idx])
        city     = m.group(1).strip()
        zip_code = m.group(2).strip()

        # Find address: scan back from city_idx
        address     = ""
        clinic_name = ""
        j = city_idx - 1

        # Skip suite continuations (short lines like "200" or "Ste 200")
        while j >= 0 and re.match(r"^(Ste|Suite|Floor|Bldg|Unit|Apt)?\s*\d+[A-Z]?$",
                                    all_lines[j], re.I):
            j -= 1

        if j >= 0 and ADDRESS_RE.match(all_lines[j]):
            address = all_lines[j]
            # Clinic name: the line(s) just before the address
            # Collect up to 3 non-address, non-city, non-credential lines
            parts = []
            k = j - 1
            while k >= max(0, j - 8):
                candidate = all_lines[k]
                # Stop on hard anchors only — NOT on specialty terms,
                # because clinic names like "Andover Family Dentistry" contain
                # medical words that would otherwise cut off the name.
                # But DO stop on: hospital affiliation lines (contain " MN" mid-line
                # which indicates "Hospital Name  City, MN"), county headers,
                # MN city refs like "Minneapolis, MN", and standalone city headers
                # that appear as section dividers in the dental section.
                if (CREDENTIAL_RE.search(candidate) or
                        ADDRESS_RE.match(candidate) or
                        CITY_LINE_RE.match(candidate) or
                        PHONE_RE.match(candidate) or
                        re.match(r"^\d", candidate) or
                        re.search(r"\bMN\s*$", candidate) or   # "Minneapolis, MN" or "Center  Princeton, MN"
                        re.match(r"^[A-Z][a-z]+ County$", candidate) or  # "Anoka County"
                        candidate == city):  # standalone city header (e.g. "Blaine" before list of Blaine clinics)
                    break
                parts.insert(0, candidate)
                k -= 1
            # Join multi-line clinic names (some span 3-4 lines)
            clinic_name = " ".join(parts).strip()
            # Strip trailing legal suffixes (PLLC, LLC, PA, Inc) and their preceding comma
            clinic_name = re.sub(r",?\s*(PLLC|PLC|LLC|P\.?A\.?|Inc\.?)$", "", clinic_name, flags=re.I).strip()
            # Strip trailing partial address fragments (e.g. "5320" from "Park Nicollet Clinic Bloomington 5320")
            clinic_name = re.sub(r"\s+\d{3,5}$", "", clinic_name).strip()

        # Strip DUPLICATED city prefix only: "Andover Andover Family Dentistry" -> "Andover Family Dentistry"
        # Do NOT strip "Andover Family Dentistry" — that's a valid clinic name
        if clinic_name and city:
            escaped = re.escape(city)
            clinic_name = re.sub(rf"^{escaped}\s+(?={escaped}\b)", "", clinic_name).strip()


        # Range of lines AFTER the city line that belong to this clinic
        # (until the next clinic's address, or end of file)
        if ci + 1 < len(city_indices):
            # End just before the next city line's address line
            next_city = city_indices[ci + 1]
            # Go back from next city to find where its address starts
            end_idx = next_city
            jj = next_city - 1
            while jj >= city_idx and re.match(r"^(Ste|Suite|Floor|Bldg|Unit|Apt)?\s*\d+[A-Z]?$",
                                               all_lines[jj], re.I):
                jj -= 1
            if jj >= city_idx and ADDRESS_RE.match(all_lines[jj]):
                end_idx = jj  # stop before the next clinic's address
        else:
            end_idx = n

        clinic_blocks.append({
            "clinic_name": clinic_name,
            "address":     address,
            "city":        city,
            "zip":         zip_code,
            "start":       city_idx + 1,
            "end":         end_idx,
        })

    # Collect providers within each clinic block
    for block in clinic_blocks:
        current_specialty = ""
        pending = []

        def flush(blk=block, spec_ref=[""]):
            nonlocal pending, current_specialty
            spec_ref[0] = current_specialty
            if not pending:
                return
            full = " ".join(pending)
            pending = []
            parsed = parse_name(full)
            if parsed:
                first, last, creds = parsed
                records.append({
                    "source":      source_label,
                    "clinic_name": blk["clinic_name"],
                    "address":     blk["address"],
                    "city":        blk["city"],
                    "state":       "MN",
                    "zip":         blk["zip"],
                    "specialty":   spec_ref[0],
                    "last_name":   last,
                    "first_name":  first,
                    "credentials": creds,
                    "accepting":   "",
                })

        # Ugh — closures in loops need care; use a helper instead
        def add_record(full_name, clinic, city, zip_code, specialty):
            parsed = parse_name(full_name)
            if parsed:
                first, last, creds = parsed
                records.append({
                    "source":      source_label,
                    "clinic_name": clinic,
                    "address":     block["address"],
                    "city":        city,
                    "state":       "MN",
                    "zip":         zip_code,
                    "specialty":   specialty,
                    "last_name":   last,
                    "first_name":  first,
                    "credentials": creds,
                    "accepting":   "",
                })

        pending = []

        for line in all_lines[block["start"]:block["end"]]:
            if PHONE_RE.match(line):
                continue
            if re.match(r"^Accepting New Patients", line, re.I):
                continue

            if is_specialty_header(line):
                if pending:
                    joined = " ".join(pending)
                    add_record(joined, block["clinic_name"], block["city"],
                               block["zip"], current_specialty)
                    pending = []
                current_specialty = line
                continue

            if CREDENTIAL_RE.search(line):
                if pending and pending[-1].endswith(","):
                    pending.append(line)
                    if not line.endswith(","):
                        joined = " ".join(pending)
                        add_record(joined, block["clinic_name"], block["city"],
                                   block["zip"], current_specialty)
                        pending = []
                else:
                    if pending:
                        joined = " ".join(pending)
                        add_record(joined, block["clinic_name"], block["city"],
                                   block["zip"], current_specialty)
                        pending = []
                    if line.endswith(","):
                        pending = [line]
                    else:
                        add_record(line, block["clinic_name"], block["city"],
                                   block["zip"], current_specialty)
                continue

            # Wrapped continuation
            if pending and pending[-1].endswith(","):
                pending.append(line)
                joined = " ".join(pending)
                if CREDENTIAL_RE.search(joined) and not line.endswith(","):
                    add_record(joined, block["clinic_name"], block["city"],
                               block["zip"], current_specialty)
                    pending = []
                continue

            # Discard anything else (noise, specialty sub-headers, etc.)
            if pending:
                joined = " ".join(pending)
                if CREDENTIAL_RE.search(joined):
                    add_record(joined, block["clinic_name"], block["city"],
                               block["zip"], current_specialty)
                pending = []

        # Flush end of block
        if pending:
            joined = " ".join(pending)
            add_record(joined, block["clinic_name"], block["city"],
                       block["zip"], current_specialty)

    return records


def build_db(records, db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, last_name TEXT, first_name TEXT, credentials TEXT,
            clinic_name TEXT, address TEXT, city TEXT, state TEXT, zip TEXT,
            specialty TEXT, accepting TEXT
        )
    """)
    c.execute("CREATE INDEX idx_last_city ON providers(last_name, city)")
    c.execute("CREATE INDEX idx_clinic    ON providers(clinic_name)")
    c.execute("CREATE INDEX idx_city      ON providers(city)")
    c.executemany("""
        INSERT INTO providers (source,last_name,first_name,credentials,clinic_name,
          address,city,state,zip,specialty,accepting)
        VALUES (:source,:last_name,:first_name,:credentials,:clinic_name,
          :address,:city,:state,:zip,:specialty,:accepting)
    """, records)
    conn.commit()
    counts = {}
    for col in ["total","medical","dental","clinics"]:
        if col == "total":
            c.execute("SELECT COUNT(*) FROM providers")
        elif col == "clinics":
            c.execute("SELECT COUNT(DISTINCT clinic_name) FROM providers")
        else:
            c.execute(f"SELECT COUNT(*) FROM providers WHERE source='{col}'")
        counts[col] = c.fetchone()[0]
    conn.close()
    return counts


def upload_to_r2(db_path):
    missing = [k for k in ["R2_ACCESS_KEY_ID","R2_SECRET_ACCESS_KEY","R2_ENDPOINT_URL"]
               if not os.environ.get(k)]
    if missing:
        print(f"  WARNING: Missing env vars {missing} — skipping upload")
        return False
    s3 = boto3.client("s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"), region_name="auto")
    print(f"  Uploading to R2 '{R2_BUCKET}/{R2_KEY}' ...")
    s3.upload_file(db_path, R2_BUCKET, R2_KEY)
    print(f"  Done — {os.path.getsize(db_path)/1024/1024:.2f} MB")
    return True


def load_env(path=".env"):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    load_env()

    if not os.path.exists(PDF_PATH):
        print(f"ERROR: PDF not found: {PDF_PATH}")
        print("Run from: C:\\Users\\Mudd\\medicare_drug_engine\\")
        sys.exit(1)

    print("=" * 60)
    print("HealthPartners Provider DB Builder — 2026")
    print("=" * 60)

    print(f"\n[1/4] Medical pages {MEDICAL_START}–{MEDICAL_END}...")
    med_pages = extract_text_pages(PDF_PATH, MEDICAL_START, MEDICAL_END)
    med_records = parse_section(med_pages, "medical")
    print(f"      {len(med_records):,} records")

    print(f"\n[2/4] Dental pages {DENTAL_START}–{DENTAL_END}...")
    dent_pages = extract_text_pages(PDF_PATH, DENTAL_START, DENTAL_END)
    dent_records = parse_section(dent_pages, "dental")
    print(f"      {len(dent_records):,} records")

    print(f"\n[3/4] Building database...")
    counts = build_db(med_records + dent_records, DB_PATH)

    print(f"\n{'='*60}")
    print(f"  Total     : {counts['total']:,}")
    print(f"  Medical   : {counts['medical']:,}")
    print(f"  Dental    : {counts['dental']:,}")
    print(f"  Clinics   : {counts['clinics']:,}")
    print(f"  Size      : {os.path.getsize(DB_PATH)/1024/1024:.2f} MB")
    print(f"{'='*60}")

    conn = sqlite3.connect(DB_PATH)
    print("\nSpot check — medical:")
    for r in conn.execute("SELECT first_name,last_name,credentials,clinic_name,city FROM providers WHERE source='medical' LIMIT 8"):
        print(f"  {r[0]:18} {r[1]:20} {r[2]:10} | {r[3][:35]:35} | {r[4]}")

    print("\nSpot check — dental:")
    for r in conn.execute("SELECT first_name,last_name,credentials,clinic_name,city FROM providers WHERE source='dental' LIMIT 8"):
        print(f"  {r[0]:18} {r[1]:20} {r[2]:10} | {r[3][:35]:35} | {r[4]}")

    print("\nSanity — Roseville medical:")
    for r in conn.execute("SELECT first_name,last_name,credentials,clinic_name FROM providers WHERE city='Roseville' AND source='medical' LIMIT 5"):
        print(f"  {r[0]:18} {r[1]:20} {r[2]:10} | {r[3][:40]}")
    conn.close()

    print("\n[4/4] Uploading to R2...")
    upload_to_r2(DB_PATH)
    print("\nDone.")
