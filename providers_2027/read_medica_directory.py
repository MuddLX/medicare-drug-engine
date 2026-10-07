"""Read the Medica 2027 provider directory PDF (Medica Advantage PPO) into providers_2027.db.

Layout (checked on pages 27 and 400): 4 columns split by vertical rules; dark-grey boxes = state
("MINNESOTA (CONTINUED)") or a specialty section; light-grey boxes = city; bold = county, clinic name
or specialty sub-heading; regular = address, phone, "Primary Health System:", "Languages:",
"Providers:" and the clinicians ("Danielson MD, Matthew A", "*" = not accepting new patients).
Plan icons (ADA ADR ...) are Arial glyphs.

    python providers_2027/read_medica_directory.py <first page> <last page>     (1-based, inclusive)
Run in chunks (about 0.4 s a page); the first chunk (first page 1) recreates the table.
"""
import os
import re
import sqlite3
import sys

import pdfplumber

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF = os.path.join(HERE, "Medica 2027 Provider Directory.pdf")
DB = os.path.join(HERE, "providers_2027.db")
SOURCE = "Medica 2027 Provider Directory (Medica Advantage PPO)"

CRED = (r"MD|DO|FNP|NP|PA|PA-C|APRN|CNP|CNS|CNM|AGNP|AGACNP|ANP|GNP|PMHNP|ACNP|WHNP|DNP|DPM|OD|DDS|DMD|PhD|PsyD|"
        r"LICSW|LCSW|LP|LMFT|LPCC|LADC|DC|PT|DPT|OT|OTR|SLP|CCC-SLP|AuD|RD|LD|CRNA|MBBS|MSW|MA|MS|RN|PharmD|LAc|LMT")
PROVIDER = re.compile(rf"^(?P<last>[A-Z][A-Za-z'’\-\. ]+?) (?P<cred>(?:{CRED})(?:[ ,]+(?:{CRED}))*),\s*(?P<first>.*)$")
CITY_ZIP = re.compile(r"^(?P<city>.+), (?P<st>[A-Z]{2})\s*(?P<zip>\d{5})?(-\d{4})?$")
PHONE = re.compile(r"^\(\d{3}\) \d{3}-\d{4}")
STATES = ("MINNESOTA", "WISCONSIN", "IOWA", "NORTH DAKOTA", "SOUTH DAKOTA")


def column_lines(page):
    """[(column, top, text, bold, box)] in reading order. box: 'dark' | 'light' | None."""
    xs = sorted(r["x0"] for r in page.rects if r["x1"] - r["x0"] < 3 and r["bottom"] - r["top"] > 300)
    edges = [0] + xs + [page.width]
    boxes = []
    for r in page.rects:
        c = r.get("non_stroking_color")
        if isinstance(c, (list, tuple)) and len(c) == 3 and r["bottom"] - r["top"] > 8 and r["x1"] - r["x0"] > 60:
            if c[0] < 0.1:
                boxes.append((r, "spec"))        # black: specialty section
            elif c[0] < 0.6:
                boxes.append((r, "dark"))        # dark grey: state
            elif c[0] < 0.9:
                boxes.append((r, "light"))
    words = page.extract_words(extra_attrs=["fontname"], keep_blank_chars=False)
    lines = {}
    for w in words:
        if w["top"] > page.height - 70 or w["top"] < 60:          # footer / page title
            continue
        col = max(i for i, e in enumerate(edges[:-1]) if w["x0"] >= e - 2)
        key = (col, round(w["top"] / 3))
        lines.setdefault(key, []).append(w)
    out = []
    for (col, _), ws in lines.items():
        ws.sort(key=lambda w: w["x0"])
        top = min(w["top"] for w in ws)
        text = " ".join(w["text"] for w in ws).strip()
        fonts = [w["fontname"] for w in ws]
        if all("Arial" in f for f in fonts):
            text = "@CODES " + text
        bold = sum("Bold" in f for f in fonts) > len(fonts) / 2
        box = None
        cx = (ws[0]["x0"] + ws[-1]["x1"]) / 2
        for bi, (r, kind) in enumerate(boxes):
            if r["x0"] - 2 <= cx <= r["x1"] + 2 and r["top"] - 2 <= top <= r["bottom"]:
                box = (kind, bi)
        out.append((col, top, text, bold, box))
    out.sort(key=lambda x: (x[0], x[1]))
    merged = []
    for item in out:
        if merged and item[4] and merged[-1][4] == item[4] and merged[-1][0] == item[0]:
            c0, t0, txt, b0, bx = merged[-1]
            merged[-1] = (c0, t0, txt + " " + item[2], b0, bx)
        else:
            merged.append(item)
    return [(c, t, txt, b, bx[0] if bx else None) for c, t, txt, b, bx in merged]


def main(first, last):
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    if first == 1:
        conn.execute("DELETE FROM directory WHERE source=?", (SOURCE,)) if conn.execute(
            "SELECT name FROM sqlite_master WHERE name='directory'").fetchone() else None
    conn.execute("""CREATE TABLE IF NOT EXISTS directory (
        source TEXT, carrier TEXT, plan_scope TEXT, section TEXT, state TEXT, county TEXT, city TEXT,
        clinic_name TEXT, address TEXT, zip TEXT, phone TEXT, health_system TEXT, plan_codes TEXT,
        specialty TEXT, last_name TEXT, first_name TEXT, credentials TEXT, accepting TEXT, page INTEGER)""")
    st = conn.execute("SELECT value FROM reader_state WHERE source=?", (SOURCE,)).fetchone() if conn.execute(
        "SELECT name FROM sqlite_master WHERE name='reader_state'").fetchone() and first != 1 else None
    import json
    s = json.loads(st[0]) if st else dict(section="PRIMARY CARE", state="MN", county="", city="", clinic="",
                                         address="", zip="", phone="", phs="", codes="", specialty="", mode="")
    rows, pending_bold, last_prov = [], [], None

    def emit_clinic_only():
        if s["clinic"] and not s.get("_had_provider"):
            rows.append(row(None, None, None, None))

    def row(lastn, firstn, cred, acc):
        return (SOURCE, "Medica", "Medica Advantage PPO", s["section"], s["state"], s["county"], s["city"],
                s["clinic"], s["address"].strip(), s["zip"], s["phone"], s["phs"].strip(), s["codes"].strip(),
                s["specialty"] or s.get("sec_spec", ""), lastn, firstn, cred, acc, page_no)

    def flush_bold(next_text):
        """A run of bold lines: county, clinic name, or specialty sub-heading (decided by the next line)."""
        nonlocal pending_bold
        if not pending_bold:
            return
        text = " ".join(pending_bold).strip()
        pending_bold = []
        if re.search(r"\bCOUNTY\b", text) and text.upper() == text:
            s["county"] = re.sub(r"\s*COUNTY.*$", "", text).title()
            s["mode"] = ""
            return
        if next_text and (next_text[:1].isdigit() or next_text.startswith(("PO Box", "P.O.", "One ", "Two "))):
            emit_clinic_only()
            s.update(clinic=text, address="", zip="", phone="", phs="", codes="",
                     specialty="", mode="address", _had_provider=False)
        else:
            s["specialty"] = re.sub(r"\s*\(CONTINUED\)", "", text, flags=re.I).title()
            s["mode"] = "providers"

    for page_no in range(first, last + 1):
        page = pdf.pages[page_no - 1]
        title = (page.extract_text(y0=0) or "")[:200] if False else ""
        top_words = [w["text"] for w in page.extract_words() if w["top"] < 60]
        head = " ".join(top_words).upper()
        prev_section = s["section"]
        if "PRIMARY CARE" in head:
            s["section"] = "PRIMARY CARE"
        elif "SPECIALTY" in head:
            s["section"] = "SPECIALTY CARE"
        elif "HOSPITAL" in head:
            s["section"] = "HOSPITALS"
        elif head.strip():
            s["section"] = head.strip().title()[:60]
        if s["section"] != prev_section:
            s["sec_spec"] = ""
        lines = column_lines(page)
        for i, (col, top, text, bold, box) in enumerate(lines):
            nxt = next((t for _, _, t, b, bx in lines[i + 1:] if not b and not bx), "")
            if box == "spec":
                flush_bold(None)
                spec = re.sub(r"\s*\(CONTINUED\)", "", text, flags=re.I).strip()
                spec = re.sub(r"(\w)- ([A-Za-z])", lambda m: m.group(1) + m.group(2).lower(), spec).replace("/ ", "/")
                s["sec_spec"] = spec.title()
                continue
            if box == "dark":
                flush_bold(None)
                t = re.sub(r"\s*\(CONTINUED\)", "", text, flags=re.I).strip()
                if any(t.upper().startswith(x) for x in STATES):
                    s["state"] = {"MINNESOTA": "MN", "WISCONSIN": "WI", "IOWA": "IA", "NORTH DAKOTA": "ND",
                                  "SOUTH DAKOTA": "SD"}.get(t.upper(), t[:2])
                else:
                    s["sec_spec"] = t.title()            # specialty section heading (specialty pages)
                continue
            if box == "light":
                flush_bold(None)
                s["city"] = re.sub(r"\s*\(Continued\)", "", text, flags=re.I).strip()
                continue
            if bold and not text.startswith(("Primary Health System", "Languages", "Providers")):
                pending_bold.append(text)
                continue
            flush_bold(text)
            if text.startswith("@CODES"):
                s["codes"] = (s["codes"] + " " + text[7:]).strip()
                s["mode"] = "codes"
                continue
            if text.startswith("Primary Health System"):
                s["phs"] = text.split(":", 1)[1].strip() if ":" in text else ""
                s["mode"] = "phs"
                continue
            if text.startswith("Languages"):
                s["mode"] = "languages"
                continue
            if text.startswith("Providers"):
                s["mode"] = "providers"
                rest = text.split(":", 1)[1].strip() if ":" in text else ""
                if not rest:
                    continue
                text = rest
            if s["mode"] == "address":
                m = CITY_ZIP.match(text)
                if PHONE.match(text):
                    s["phone"] = text[:14]
                elif m:
                    s["zip"] = m.group("zip") or ""
                elif re.fullmatch(r"\d{5}(-\d{4})?", text):
                    s["zip"] = text[:5]
                elif not s["phone"]:
                    s["address"] += " " + text
                continue
            if s["mode"] == "phs":
                s["phs"] += " " + text
                continue
            if s["mode"] == "languages" and not PROVIDER.match(text):
                continue
            m = PROVIDER.match(text)
            if m:
                first_n = m.group("first").strip()
                acc = "N" if first_n.endswith("*") or text.endswith("*") else "Y"
                last_prov = [m.group("last").strip(), first_n.rstrip("*").strip(), m.group("cred").replace(" ", ""), acc]
                rows.append(row(*last_prov))
                s["_had_provider"] = True
                s["mode"] = "providers"
            elif s["mode"] == "providers" and rows and rows[-1][14] and len(text) < 30:
                r = list(rows[-1])                    # first name wrapped onto the next line
                r[15] = (r[15] + " " + text.rstrip("*")).strip()
                if text.endswith("*"):
                    r[17] = "N"
                rows[-1] = tuple(r)
    flush_bold(None)
    conn.executemany("INSERT INTO directory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("CREATE TABLE IF NOT EXISTS reader_state (source TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO reader_state VALUES (?,?)", (SOURCE, json.dumps(s)))
    conn.commit()
    print(f"pages {first}-{last}: {len(rows)} rows; total {conn.execute('SELECT COUNT(*) FROM directory WHERE source=?', (SOURCE,)).fetchone()[0]}")


if __name__ == "__main__":
    pdf = pdfplumber.open(PDF)
    a = int(sys.argv[1]); b = min(int(sys.argv[2]), len(pdf.pages))
    main(a, b)
