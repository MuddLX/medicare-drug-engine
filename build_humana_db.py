from pypdf import PdfReader
import re, sqlite3, os, sys, glob
import boto3
from botocore.client import Config

PDF_FOLDER = "Humana Provider PDFs"
DB_PATH    = "humana_providers.db"
R2_BUCKET  = "medicare-db"
R2_KEY     = "humana_providers.db"

DENTAL_SPECIALTY_RE = re.compile(r'^Dentist', re.IGNORECASE)

SKIP_RE = re.compile(
    r'^(Vision Providers|Pharmacies|Network Pharmacies|Fitness Centers|'
    r'Fitness Providers|Durable Medical|Laboratory Services|'
    r'Mail Order|Index$|CLINICAL MEDICAL)',
    re.IGNORECASE
)

MENTAL_RE = re.compile(r'^(Mental Health|Substance Abuse)', re.IGNORECASE)

COUNTY_RE  = re.compile(r'^([A-Z][A-Z\s\.]+)\s+COUNTY$')
ADDRESS_RE = re.compile(
    r'^\d+\s+.+?\b(Ave|St|Rd|Blvd|Dr|Ln|Way|Pkwy|Pl|Ct|Cir|Hwy|Loop|Trl|'
    r'Sq|NW|NE|SW|SE|Row|Path|Xing|Ter|Mall|Fwy|Expy|Pike|Pass|Trce|Curv)\b', re.IGNORECASE)
CITY_RE    = re.compile(r'^(.+),\s*MN\s+(\d{5})', re.IGNORECASE)
PHONE_RE   = re.compile(r'^\(\d{3}\)\s*\d{3}-\d{4}')
PCP_RE     = re.compile(r'^PCP#\s*\w+')
TERMDATE_RE = re.compile(r'^Term Date:', re.IGNORECASE)
LANGUAGE_RE = re.compile(
    r'^(Spanish|French|German|Japanese|Chinese|Korean|Vietnamese|'
    r'Arabic|Russian|Somali|Hmong|Tagalog|Portuguese|Italian|'
    r'Polish|Hindi|Urdu|Mandarin|Cantonese|Swahili|Amharic|'
    r'Spanish,\s*German|Spanish,\s*French)$', re.IGNORECASE)

NOISE_RE = re.compile(
    r'^(\*Board Certified|This page was intentionally|Esta p|'
    r'Medicaid Certified|Cultural Competency|Established patients only|'
    r'Nursing Home Visits Only|Telehealth Only|NursingHome Visits Only|'
    r'\*\* Provider has|PROVIDER DIRECTORY|HumanaChoice PPO Provider|'
    r'This directory is current|This directory provides|'
    r'To access Humana|To request a hard copy|'
    r'If you request|If you notice|Customer Care contact|'
    r'This information is available|Este documento|'
    r'MINNESOTA$|URGENT CARE CENTERS|PRIMARY CARE PROVIDERS|'
    r'VISION PROVIDERS|PHARMACIES|FITNESS CENTERS|'
    r'MENTAL HEALTH|HOSPITALS$|SPECIALISTS$|'
    r'OTHER HEALTH CARE PROVIDERS|DURABLE MEDICAL|LABORATORY)',
    # NOTE: removed DENTAL PROVIDERS from noise — it's a page header we need to pass through
    re.IGNORECASE
)

SPECIALTY_RE = re.compile(
    r'^(Family Medicine|Internal Medicine|General Practice|'
    r'Obstetrics|Gynecology|Pediatric|Geriatric|'
    r'Primary Care Nurse|Primary Care Physician|'
    r'Dentist|Optometrist|Ophthalmolog|'
    r'Cardiology|Neurology|Oncology|Orthopedic|Surgery|'
    r'Dermatology|Urology|Gastroenterology|Pulmonolog|'
    r'Rheumatology|Endocrinology|Nephrology|Hematology|'
    r'Psychiatry|Psychology|Social Work|'
    r'Physical Therapy|Occupational Therapy|Speech|'
    r'Chiropractic|Podiatry|Audiology|'
    r'Allergy|Immunology|Infectious|Vascular|'
    r'Hospitalist|Critical Care|Anesthesiology|'
    r'Radiology|Pathology|Emergency|'
    r'Urgent Care Clinic|Hospital \(General|Critical Access|'
    r'Psychiatric Hospital)',
    re.IGNORECASE
)

_CREDS = (
    r'MD|DO|DDS|DMD|DPM|OD|DC|PharmD|LCSW|LMFT|PhD|PsyD|AuD|'
    r'FNP-BC|FNP-C|NP-BC|NP-C|ARNP|AGPCNP|PMHNP|CPNP|WHNP|ACNP|'
    r'FNP|ANP|GNP|CNM|CNS|CRNA|APRN|PAC|NP|DNP|PA|RD|LD|MPAS|MMS'
)

CRED_END_RE        = re.compile(r'\s(' + _CREDS + r')\*?$', re.IGNORECASE)
CRED_STANDALONE_RE = re.compile(r'^(' + _CREDS + r')\*?$', re.IGNORECASE)
CRED_NOSPACE_RE    = re.compile(r'([a-zA-Z])(' + _CREDS + r')\*?(\s|$)', re.IGNORECASE)


def normalize_name_line(line):
    def inject_space(m):
        char = m.group(1); cred = m.group(2); after = m.group(3)
        if char.islower() and cred[0].isupper():
            return char + ' ' + cred + after
        return m.group(0)
    line = CRED_NOSPACE_RE.sub(inject_space, line)
    line = re.sub(r'([a-z])([A-Z])\s+(' + _CREDS + r')', r'\1 \2 \3', line)
    return line


def has_credential(line):
    return bool(CRED_END_RE.search(line))


def parse_name(line):
    line = normalize_name_line(line)
    m    = CRED_END_RE.search(line)
    if not m:
        return None
    creds     = m.group(1).strip()
    name_part = line[:m.start()].strip().rstrip(',').strip()
    name_part = re.sub(r',\s*[A-Z]\s*$', '', name_part).strip()
    if ',' in name_part:
        parts      = name_part.split(',', 1)
        last_name  = parts[0].strip()
        first_name = parts[1].strip().rstrip(',').strip()
    else:
        parts      = name_part.split()
        last_name  = parts[-1] if parts else ''
        first_name = ' '.join(parts[:-1])
    return last_name, first_name, creds


def clean_lines(text):
    out = []
    for raw in text.split('\n'):
        line = raw.strip()
        if not line: continue
        if re.match(r'^\d+$', line): continue
        if NOISE_RE.match(line): continue
        if PCP_RE.match(line): continue
        if TERMDATE_RE.match(line): continue
        if LANGUAGE_RE.match(line): continue
        if re.search(r'\.\s*\.\s*\.', line): continue
        out.append(line)
    return out


def extract_pages(pdf_path):
    reader = PdfReader(pdf_path)
    return [(i+1, reader.pages[i].extract_text() or '')
            for i in range(len(reader.pages))]


def parse_pdf(pdf_path):
    records   = []
    all_lines = []
    for _, text in extract_pages(pdf_path):
        all_lines.extend(clean_lines(text))

    n = len(all_lines)

    active            = False
    current_source    = 'medical'
    current_county    = ''
    current_city      = ''
    current_zip       = ''
    current_address   = ''
    current_clinic    = ''
    current_specialty = ''
    clinic_parts      = []
    pending           = []

    def flush_clinic():
        nonlocal current_clinic, clinic_parts
        if clinic_parts:
            name = ' '.join(clinic_parts).strip()
            name = re.sub(r',?\s*(PLLC|PLC|LLC|P\.?A\.?|Inc\.?|PC|LTD)$',
                          '', name, flags=re.I).strip()
            current_clinic = name
            clinic_parts   = []

    def add_record(last, first, creds):
        if not last: return
        records.append({
            'source':      current_source,
            'last_name':   last,
            'first_name':  first,
            'credentials': creds,
            'clinic_name': current_clinic,
            'address':     current_address,
            'city':        current_city,
            'state':       'MN',
            'zip':         current_zip,
            'county':      current_county,
            'specialty':   current_specialty,
        })

    def flush_pending():
        nonlocal pending
        if not pending: return
        joined  = ' '.join(pending); pending = []
        parsed  = parse_name(normalize_name_line(joined))
        if parsed: add_record(*parsed)

    i = 0
    while i < n:
        line = all_lines[i]

        # ── Activation ────────────────────────────────────────────────────
        if line == 'List of Network Providers':
            active = True; i += 1; continue

        if not active:
            i += 1; continue

        # ── County header — ALWAYS processed, even in skip mode ───────────
        # This is the key fix: county breaks us out of skip mode
        m = COUNTY_RE.match(line)
        if m:
            flush_pending(); flush_clinic()
            current_county    = m.group(1).strip().title() + ' County'
            current_city      = ''
            current_zip       = ''
            current_address   = ''
            current_clinic    = ''
            current_specialty = ''
            # Break out of skip mode on new county
            # (dental stays dental, skip becomes medical)
            if current_source == 'skip':
                current_source = 'medical'
            i += 1; continue

        # ── Skip sections ─────────────────────────────────────────────────
        if SKIP_RE.match(line):
            flush_pending(); flush_clinic()
            current_source = 'skip'
            i += 1; continue

        if current_source == 'skip':
            i += 1; continue

        # ── "Dental Providers" page header — ignore, just a running header ─
        if re.match(r'^Dental Providers$', line, re.IGNORECASE):
            i += 1; continue

        # ── Mental health ─────────────────────────────────────────────────
        if MENTAL_RE.match(line):
            flush_pending(); flush_clinic()
            current_source    = 'medical'
            current_specialty = 'Mental Health'
            i += 1; continue

        if line == 'MINNESOTA':
            i += 1; continue

        # ── City, MN ZIP ──────────────────────────────────────────────────
        m = CITY_RE.match(line)
        if m:
            flush_pending()
            current_city = m.group(1).strip()
            current_zip  = m.group(2).strip()
            flush_clinic()
            i += 1; continue

        # ── Street address ────────────────────────────────────────────────
        if ADDRESS_RE.match(line):
            flush_pending()
            current_address = line
            i += 1; continue

        # ── Phone — closes clinic block ───────────────────────────────────
        if PHONE_RE.match(line):
            flush_pending(); flush_clinic()
            i += 1; continue

        # ── Specialty header ──────────────────────────────────────────────
        if SPECIALTY_RE.match(line):
            flush_pending(); flush_clinic()
            current_specialty = line
            if DENTAL_SPECIALTY_RE.match(line):
                current_source = 'dental'
            i += 1; continue

        # ── Specialty continuations ───────────────────────────────────────
        if line in ('(PCP)', 'Assistant', 'Practitioner'):
            current_specialty = (current_specialty + ' ' + line).strip()
            i += 1; continue

        # ── Pending name accumulation / flush ─────────────────────────────
        if pending:
            test = normalize_name_line(' '.join(pending) + ' ' + line)
            if has_credential(test):
                parsed = parse_name(test)
                if parsed:
                    pending = []; add_record(*parsed)
                    i += 1; continue
            if CRED_STANDALONE_RE.match(line):
                pending.append(line)
                i += 1; continue
            flush_pending()

        # ── Provider name with credential ─────────────────────────────────
        norm = normalize_name_line(line)
        if has_credential(norm):
            parsed = parse_name(norm)
            if parsed: add_record(*parsed)
            i += 1; continue

        # ── Start of wrapped name ─────────────────────────────────────────
        if re.match(r'^[A-Z][a-zA-Z\-\']+,\s+[A-Z]', line):
            pending = [line]
            i += 1; continue

        # ── Clinic name ───────────────────────────────────────────────────
        if not re.match(
            r'^(Unit\s+\d|Suite\s+\d|Ste\s+\d|Apt\s+\d|\d+$|'
            r'Hospitals$|Urgent Care Centers|Primary Care Providers$|'
            r'Specialists$|Other Health Care|DENTAL PROVIDERS|'
            r'Dental Providers$)', line, re.I
        ):
            clinic_parts.append(line)
        i += 1

    flush_pending()
    return records


def build_db(records, db_path):
    if os.path.exists(db_path): os.remove(db_path)
    conn = sqlite3.connect(db_path)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, last_name TEXT, first_name TEXT, credentials TEXT,
            clinic_name TEXT, address TEXT, city TEXT, state TEXT,
            zip TEXT, county TEXT, specialty TEXT
        )
    """)
    c.execute("CREATE INDEX idx_last_city ON providers(last_name, city)")
    c.execute("CREATE INDEX idx_clinic    ON providers(clinic_name)")
    c.execute("CREATE INDEX idx_city      ON providers(city)")
    c.execute("CREATE INDEX idx_county    ON providers(county)")
    c.executemany("""
        INSERT INTO providers (source,last_name,first_name,credentials,
          clinic_name,address,city,state,zip,county,specialty)
        VALUES (:source,:last_name,:first_name,:credentials,
          :clinic_name,:address,:city,:state,:zip,:county,:specialty)
    """, records)
    conn.commit()
    counts = {}
    for key, sql in [
        ('total',   'SELECT COUNT(*) FROM providers'),
        ('medical', "SELECT COUNT(*) FROM providers WHERE source='medical'"),
        ('dental',  "SELECT COUNT(*) FROM providers WHERE source='dental'"),
        ('cities',  'SELECT COUNT(DISTINCT city) FROM providers'),
        ('clinics', "SELECT COUNT(DISTINCT clinic_name) FROM providers WHERE clinic_name != ''"),
    ]:
        counts[key] = c.execute(sql).fetchone()[0]
    conn.close()
    return counts


def upload_to_r2(db_path):
    missing = [k for k in ['R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_ENDPOINT_URL']
               if not os.environ.get(k)]
    if missing:
        print('  WARNING: Missing env vars %s - skipping upload' % missing)
        return False
    s3 = boto3.client('s3',
        endpoint_url=os.environ['R2_ENDPOINT_URL'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4'), region_name='auto')
    print("  Uploading to R2 '%s/%s' ..." % (R2_BUCKET, R2_KEY))
    s3.upload_file(db_path, R2_BUCKET, R2_KEY)
    print('  Done - %.2f MB' % (os.path.getsize(db_path)/1024/1024))
    return True


def load_env(path='.env'):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())


if __name__ == '__main__':
    load_env()

    if not os.path.exists(PDF_FOLDER):
        print('ERROR: Folder not found:', PDF_FOLDER); sys.exit(1)

    pdfs = sorted(glob.glob(os.path.join(PDF_FOLDER, '*.pdf')))
    if not pdfs:
        print('ERROR: No PDFs found'); sys.exit(1)

    print('=' * 60)
    print('Humana Provider DB Builder - 2026')
    print('=' * 60)
    print('Found %d PDFs\n' % len(pdfs))

    all_records = []
    for pdf_path in pdfs:
        name = os.path.basename(pdf_path)
        print('  Parsing:', name)
        try:
            recs = parse_pdf(pdf_path)
            print('           %s records' % format(len(recs), ','))
            all_records.extend(recs)
        except Exception as e:
            print('  ERROR:', e)
            import traceback; traceback.print_exc()

    print('\nTotal raw: %s' % format(len(all_records), ','))
    print('Deduplicating...')
    seen = set(); deduped = []
    for r in all_records:
        key = (r['last_name'].lower(), r['first_name'].lower(),
               r['city'].lower(), r['source'])
        if key not in seen:
            seen.add(key); deduped.append(r)
    print('After dedup: %s' % format(len(deduped), ','))

    print('\nBuilding database...')
    counts = build_db(deduped, DB_PATH)

    print('\n' + '='*60)
    print('  Total   : %s' % format(counts['total'],   ','))
    print('  Medical : %s' % format(counts['medical'], ','))
    print('  Dental  : %s' % format(counts['dental'],  ','))
    print('  Cities  : %s' % format(counts['cities'],  ','))
    print('  Clinics : %s' % format(counts['clinics'], ','))
    print('  Size    : %.2f MB' % (os.path.getsize(DB_PATH)/1024/1024))
    print('='*60)

    conn = sqlite3.connect(DB_PATH)
    print('\nSpot check - medical:')
    for r in conn.execute(
        "SELECT first_name,last_name,credentials,clinic_name,city "
        "FROM providers WHERE source='medical' LIMIT 8"
    ):
        print('  %-16s %-20s %-8s | %-35s | %s' % (r[0],r[1],r[2],r[3][:35],r[4]))

    print('\nSpot check - dental:')
    for r in conn.execute(
        "SELECT first_name,last_name,credentials,clinic_name,city "
        "FROM providers WHERE source='dental' LIMIT 8"
    ):
        print('  %-16s %-20s %-8s | %-35s | %s' % (r[0],r[1],r[2],r[3][:35],r[4]))

    conn.close()

    print('\nUploading to R2...')
    upload_to_r2(DB_PATH)
    print('\nDone.')
