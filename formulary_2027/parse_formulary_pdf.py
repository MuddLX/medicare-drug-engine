"""Read the drug table out of a carrier's 2027 formulary PDF (CMS standard template:
Drug Name | Drug Tier | Requirements/Limits). Handles wrapped names, wrapped limits, category
headings, the index at the back, and side-by-side (two-column) pages (HealthSpring).
Output: one JSON per PDF with every drug row: name text, tier, limits, page.
Read-only on the PDFs. Usage: python formulary_2027/parse_formulary_pdf.py "<pdf>" "<out.json>"
"""
import json, re, subprocess, sys

ROW = re.compile(r"^(?P<lead>\s*)(?P<name>\S.*?\S)(?P<gap>\s{2,})(?:(?P<bg>[BG])\s+)?(?P<tier>[1-6])[\^*#+]?(?:\s{2,}(?P<limits>\S.*))?$")
ROW_TIGHT = re.compile(r"^(?P<lead>\s*)(?P<name>\S.*?\S) (?P<tier>[1-6])[\^*#+]?(?:\s{2,}(?P<limits>\S.*))?$")  # name runs into the tier column
LIMIT_CODE = re.compile(r"(PA|QL|ST|LD|LA|MO|NDS|HRM|ACS|DL|B/D|BvsD|EX|NM|SP|GC|OTC|LTC|B|G|\^|\*)")
HEADER = re.compile(r"Drug\s+Name.*(Drug\s+Tier|Requirements|Coverage rules|Drug\b)", re.I)
NOISE = re.compile(r"(Last Updated|Formulary ID|Submission ID|symbols and abbreviations|this table mean|"
                   r"^this table\.?$|going to page|^\d{1,3}$|^Page \d|Version \d|Updated on|^[IVX]+-\d+$|"
                   r"\.{5,}|CAPITALIZED = BRAND|Lowercase italic|italic = Generic|^Lower$|^case$|^\d{2}/\d{2}/\d{4}\s+\d+$|Covered Drugs by Category|^Limits$|^[A-Z]\d$|^Brand$|^or$|^Generic tier|^Last updated|^\d+\s+Last updated|^(\d+\s+)?Updated \d{2}/\d{2}/\d{4}|effective \d{2}/\d{2}/\d{4})", re.I)
FORM_WORDS = re.compile(r"\b(ORAL|TABLETS?|TAB|CAPSULES?|CAP|INJ(ECTION)?|INTRAVENOUS|SUBCUTANEOUS|INTRAMUSCULAR|KIT|"
                        r"SOLN|SOLUTION|RECON|SUSP(ENSION)?|PEN|INJECTOR|PACK|CREAM|OINTMENT|GEL|PATCH|SPRAY|INHAL\w*|"
                        r"POWDER|SYRINGE|VIAL|EXTENDED|RELEASE|DELAYED|CHEWABLE|DISINTEGRATING|FOR|ER|DR|HR|"
                        r"MG|MCG|ML|GM|UNITS?|%|\d)", re.I)


def page_info(pdf):
    out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    return int(re.search(r"Pages:\s+(\d+)", out).group(1)), float(re.search(r"Page size:\s+([\d.]+)", out).group(1))


def page_text(pdf, p, x=None, w=None):
    cmd = ["pdftotext", "-layout", "-f", str(p), "-l", str(p)]
    if x is not None:
        cmd += ["-x", str(x), "-y", "0", "-W", str(w), "-H", "2000"]
    return subprocess.run(cmd + [pdf, "-"], capture_output=True, text=True).stdout


def chunks(pdf):
    """Yield (page, text) for each table column on each page, in reading order."""
    pages, width = page_info(pdf)
    for p in range(1, pages + 1):
        full = page_text(pdf, p)
        if any(len(re.findall(r"Drug\s+Name", line)) >= 2 for line in full.splitlines()):   # two tables side by side
            half = int(width / 2)
            yield p, page_text(pdf, p, 0, half)
            yield p, page_text(pdf, p, half, int(width) - half)
        else:
            yield p, full


def is_heading(s):
    """A category heading, not part of a drug name."""
    if re.search(r"\d", s):
        return False
    letters = re.sub(r"[^A-Za-z]", "", s)
    if letters.isupper():                       # ALL CAPS: heading unless it reads like a dose form
        return not FORM_WORDS.search(s)
    return s[0].isupper()                       # Title Case line with no tier and no strength


TIER_ONLY = re.compile(r"^\s*(?P<tier>[1-6])(?:\s{2,}(?P<limits>\S.*))?$")
UNIT_ONLY = re.compile(r"^(MG|MCG|ML|GM|UNITS?|%|HR|HOUR)\b", re.I)


def centered_mode(pdf):
    """True when the PDF prints the tier between the two halves of wrapped names (Wellcare)."""
    n = 0
    for _, text in chunks(pdf):
        n += sum(1 for l in text.splitlines() if TIER_ONLY.match(l) and l.strip())
        if n > 80:
            return True
    return False


def _bbox_lines(pdf, p):
    """Text lines on page p with their position: [(xMin, yMid, text)] (pdftotext -bbox-layout)."""
    import html
    out = subprocess.run(["pdftotext", "-bbox-layout", "-f", str(p), "-l", str(p), pdf, "-"],
                         capture_output=True, text=True).stdout
    lines = []
    for m in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', out, re.S):
        words = re.findall(r'<word[^>]*xMin="([\d.]+)"[^>]*>(.*?)</word>', m.group(5))
        if words:
            lines.append((float(m.group(1)), (float(m.group(2)) + float(m.group(4))) / 2,
                          [(float(x), html.unescape(w)) for x, w in words]))
    return lines


def parse_centered(pdf):
    """Wellcare layout: name, tier and limits are each vertically centred on the row, so wrapped
    text sits above AND below the tier. Using positions on the page: find the column edges from the
    header, then give every name or limits fragment to the tier number it is vertically closest to."""
    pages, _ = page_info(pdf)
    rows = []
    for p in range(1, pages + 1):
        lines = _bbox_lines(pdf, p)
        words = [(x, y, w) for _, y, ws in lines for x, w in ws]
        hdr = [(x, y) for x, y, w in words if w == "Requirements/Limits" or w.startswith("Requirements")]
        name_hdr = [(x, y) for x, y, w in words if w == "Name"]
        if not hdr or not name_hdr:
            continue
        lim_x, hdr_y = hdr[0]
        tier_words = [(x, y, w) for x, y, w in words if y > hdr_y + 4 and lim_x - 45 < x < lim_x - 2
                      and re.fullmatch(r"[1-6][\^*#+]?", w)]
        if not tier_words:
            continue
        tier_x = min(x for x, _, _ in tier_words)
        tiers = sorted((y, int(w[0])) for _, y, w in tier_words)
        frag_name = {i: [] for i in range(len(tiers))}
        frag_lim = {i: [] for i in range(len(tiers))}
        for _, y, ws in lines:
            if y <= hdr_y + 10:
                continue
            name_words = [w for x, w in ws if x < tier_x - 4]
            lim_words = [w for x, w in ws if x >= lim_x - 2]
            text_n, text_l = " ".join(name_words).strip(), " ".join(lim_words).strip()
            if text_n and NOISE.search(text_n):
                text_n = ""
            if text_l and NOISE.search(text_l):
                text_l = ""
            if not text_n and not text_l:
                continue
            i = min(range(len(tiers)), key=lambda k: abs(tiers[k][0] - y))
            if abs(tiers[i][0] - y) > 22:              # too far from any row: a heading or footer
                continue
            if text_n:
                frag_name[i].append((y, text_n))
            if text_l:
                frag_lim[i].append((y, text_l))
        for i, (y, tier) in enumerate(tiers):
            name = " ".join(t for _, t in sorted(frag_name[i]))
            if not name or is_heading(name):
                continue
            rows.append({"name": name, "tier": tier, "limits": " ".join(t for _, t in sorted(frag_lim[i])), "page": p})
    return rows


def parse(pdf):
    if centered_mode(pdf):
        return parse_centered(pdf)
    rows, started, in_index = [], False, False
    last = None            # the row a wrapped line may belong to
    tier_col = None        # where the tier digit sits in this table
    for page, text in chunks(pdf):
        for raw in text.splitlines():
            line = raw.rstrip()
            s = line.strip()
            if not s:
                continue
            if HEADER.search(s):
                started, last = True, None
                continue
            if not started:
                continue
            if s.lower() == "tier":
                continue
            if re.match(r"^index\b", s, re.I) and len(rows) > 200:   # back index starts (check before dot-leaders)
                in_index = True
            if re.search(r"\.\s?\.{3,}", s):                 # dot-leader index line (front or back)
                continue
            if in_index or NOISE.search(s):
                continue
            indent = len(line) - len(line.lstrip())
            line = re.sub(r"(\s[BG]\s+[1-6])([A-Z]{2})", r"\1  \2", line)   # "B  5DL; QL" -> tier 5, limits "DL; QL"
            m = ROW.match(line)
            if not m and tier_col is not None:
                t = ROW_TIGHT.match(line)
                off = abs(len(t.group("lead")) + len(t.group("name")) + 1 - tier_col) if t else 99
                # close to the tier column, or a bit further when a limits column follows (Aetna)
                if off <= 5 or (off <= 10 and t.group("limits") and not t.group("name").rstrip().endswith(",")):
                    m = t
            if m and re.search(r"[A-Za-z]", m.group("name")):
                if "gap" in m.groupdict():
                    tier_col = len(m.group("lead")) + len(m.group("name")) + len(m.group("gap"))
                last = {"name": m.group("name").strip(), "tier": int(m.group("tier")),
                        "limits": (m.group("limits") or "").strip(), "page": page, "indent": indent,
                        "bg": m.groupdict().get("bg") or ""}
                rows.append(last)
                continue
            if last is None:
                continue
            if tier_col is not None and indent >= tier_col - 1:            # under the tier/limits columns
                last["limits"] = (last["limits"] + " " + s).strip()
                continue
            no_strength_yet = not re.search(r"\d", last["name"])
            open_paren = last["name"].count("(") > last["name"].count(")")   # "(Oral Tablet ... 24" / "Hour)"
            wraps = (open_paren or last["name"].endswith((",", "-", "/", "(", "&", ";"))
                     or s[0].islower() or s[0].isdigit() or s[0] in "(#*"
                     or indent > last["indent"]
                     or (not is_heading(s)))
            if wraps or (no_strength_yet and not is_heading(s)):
                last["name"] = (last["name"] + " " + s).strip()
            else:
                last = None                                   # a heading: nothing wraps onto it
    for r in rows:
        r.pop("indent", None)
    return rows


if __name__ == "__main__":
    pdf, out = sys.argv[1], sys.argv[2]
    rows = parse(pdf)
    for r in rows:   # limit codes that sat in the limits column while the name wrapped
        parts = re.split(r"\s{4,}", r["name"].strip())
        if len(parts) > 1:
            name, lims = [parts[0]], []
            for part in parts[1:]:
                toks = part.split()
                while toks and LIMIT_CODE.fullmatch(toks[0].rstrip(";,")):
                    lims.append(toks.pop(0))
                name.extend(toks)
            r["name"] = " ".join(name)
            if lims:
                r["limits"] = (r["limits"] + " " + " ".join(lims)).strip()
    for r in rows:   # footer text that slipped into a wrapped name
        r["name"] = re.sub(r"\s+20\d\d$", "", r["name"])   # stray footer year (Blue Cross / MedicareBlue)
        r["name"] = re.sub(r"\s*(\d+\s+)?Updated \d{2}/\d{2}/\d{4}.*$|\s{3,}\d+\s+\d{4}\s+\S+\s+\d+\s+v\d+\s+effective.*$", "", r["name"]).strip()
    json.dump(rows, open(out, "w", encoding="utf-8"), indent=1)
    from collections import Counter
    print(f"{len(rows)} drug rows | tiers {dict(sorted(Counter(r['tier'] for r in rows).items()))} | pages {rows[0]['page'] if rows else '-'}-{rows[-1]['page'] if rows else '-'}")
