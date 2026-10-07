"""Health-system level network map for 2027 (2026-10-06, Jordon): which carriers include which systems/clinics.

Sources (2027 only - older data is deliberately not used):
  * UHC: the agency AEP sheet's "MAPD Networks" tab, UHC column, taken from UHC's 2027 MN-0002, FG-0001 and
    MN-8 directories (Aug 10, 2026); MN-0001 uses the MN-0002 network per the agency. X = in network
    (system level), VERIFY = not found as an organization -> "verify", never "out of network".
  * Medica: the Medica 2027 directory (providers_2027.db, table directory): a system counts as in network when
    the directory lists clinics under it (as Primary Health System or by clinic name).
  * Agency confirmations (weekly lists from Jill / Lacey): providers_2027/confirmations.csv.
The other carrier columns on the sheet are 2026 and are not used.

    python providers_2027/build_systems.py
"""
import csv
import os
import re
import sqlite3

import openpyxl

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "providers_2027.db")
SHEET = os.path.join(HERE, "source_data", "providers_2027", "2027 AEP Plan Comparison Sharaeable.xlsx")
CONFIRM = os.path.join(HERE, "providers_2027", "confirmations.csv")
UHC_SOURCE = "Agency AEP sheet (UHC 2027 directories, Aug 10 2026)"
UHC_SCOPE = "UHC AARP MN-0001, MN-0002, FG-0001 (H2001-116, 117, 118)"

# Extra names a client might write, or a directory might use, for a sheet row (all lower case).
ALIASES = {
    "Allina": ["allina", "courage kenny"],
    "Abbott Northwestern Hospital": ["abbott northwestern"],
    "MHealth Fairview (including Healtheast rebranded)": ["fairview", "m health fairview", "healtheast"],
    "MHealth Fairview (U of M Hospital)": ["university of minnesota medical center", "u of m hospital", "umn medical center"],
    "MHealth Fairview (U of M Physicians Group)": ["u of m physicians", "university of minnesota physicians"],
    "Health Partners": ["healthpartners", "health partners"],
    "Park Nicollet Methodist": ["park nicollet", "methodist hospital"],
    "Hennepin Health HCMC": ["hennepin healthcare", "hcmc"],
    "HCMC Hospital": ["hennepin county medical center"],
    "Mayo": ["mayo clinic"],
    "CentraCare Health (St Cloud)": ["centracare"],
    "Essentia": ["essentia"],
    "Sanford": ["sanford"],
    "Avera": ["avera"],
    "Altru Health": ["altru"],
    "North Memorial": ["north memorial"],
    "Ridgeview Medical Center": ["ridgeview"],
    "Olmsted Medical": ["olmsted medical"],
    "Tria": ["tria orthopedic", "tria "],
    "Twin Cities Ortho": ["twin cities orthopedic"],
    "Summit": ["summit orthopedics"],
    "MNGI Digestive Health": ["mngi"],
    "Minnesota Oncology": ["minnesota oncology"],
    "Minnesota Urology": ["minnesota urology"],
    "Entira": ["entira"],
    "Stellis Health": ["stellis"],
    "Regions": ["regions hospital"],
    "St Johns (part of Mhealth)": ["st johns hospital", "st john's hospital"],
    "ST. LUKES (Duluth)": ["st lukes", "st luke's"],
    "Gunderson St. Elizabeth": ["gundersen st elizabeth", "gunderson st elizabeth"],
    "Riversedge (St. Peter)": ["river's edge", "rivers edge", "riversedge"],
    "Hutchinson Clinic": ["hutchinson health", "hutchinson clinic"],
    "Lakeview": ["lakeview clinic"],
    "Glencoe": ["glencoe regional", "glencoe"],
    "Northfield": ["northfield hospital", "northfield"],
    "Mankato Clinic": ["mankato clinic"],
    "North Clinic": ["north clinic"],
    "Bluestone": ["bluestone"],
    "Cuyuna": ["cuyuna"],
    "Herself Health": ["herself health"],
    "Woodwinds": ["woodwinds"],
    "Voyage": ["voyage healthcare"],
    "McCannel Eye": ["mccannel"],
    "Edina Eye Clinic": ["edina eye"],
    "West Metro Opthamology": ["west metro ophthalmology", "west metro opthamology"],
    "Phillips Eye Institute": ["phillips eye"],
    "St Paul Eye": ["st paul eye", "saint paul eye"],
    "Mercy Hospital": ["mercy hospital"],
    "United Hospital": ["united hospital"],
    "Unity Hospital": ["unity hospital"],
    "Maple Grove Hospital": ["maple grove hospital"],
    "St Francis Regional Medical Center": ["st francis regional"],
}


# Single hospitals / local organisations: only count a directory match in these cities (a name like
# "United Hospital" or "St Lukes" also belongs to unrelated places elsewhere).
HOME_CITIES = {
    "Buffalo Hospital": ["buffalo"], "Cambridge Medical Center": ["cambridge"], "District One Hospital": ["faribault"],
    "Hutchinson Hospital": ["hutchinson"], "Mercy Hospital": ["coon rapids", "minneapolis", "fridley"],
    "Owatonna Hospital": ["owatonna"], "Regions": ["st paul"], "Riversedge (St. Peter)": ["st peter"],
    "Sleepy Eye Medical Center": ["sleepy eye"], "St Francis Regional Medical Center": ["shakopee"],
    "ST. LUKES (Duluth)": ["duluth"], "United Hospital": ["st paul"], "Unity Hospital": ["fridley"],
}


def norm(t):
    t = (t or "").lower().replace(".", " ").replace("'", "").replace("’", "")
    t = re.sub(r"\bsaint\b", "st", t)
    return " ".join(re.sub(r"[^a-z0-9& ]", " ", t).split())


def patterns(system):
    base = [norm(system)]
    base += [norm(a) for a in ALIASES.get(system, [])]
    return [p for p in dict.fromkeys(base) if p]


def sheet_rows():
    wb = openpyxl.load_workbook(SHEET, data_only=True, read_only=True)
    ws = wb["MAPD Networks"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = next(i for i, r in enumerate(rows) if r and r[0] == "Provider / health system")
    out = []
    for r in rows[hdr + 2:]:
        name = (r[0] or "").strip() if r and r[0] else ""
        if not name or name.isupper() and "PHARMACY" in name:
            break
        out.append((name, (str(r[1]).strip() if r[1] is not None else "")))
    return out


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    systems = sheet_rows()
    conn.execute("DROP TABLE IF EXISTS systems")
    conn.execute("CREATE TABLE systems (system TEXT PRIMARY KEY, patterns TEXT)")
    conn.execute("DROP TABLE IF EXISTS system_network")
    conn.execute("""CREATE TABLE system_network (system TEXT, carrier TEXT, status TEXT, detail TEXT, plan_scope TEXT,
        source TEXT, as_of TEXT)""")
    clinics = conn.execute("SELECT DISTINCT clinic_name, health_system, city FROM directory").fetchall()
    nclin = [(norm(c), norm(h), city) for c, h, city in clinics]
    for name, uhc in systems:
        pats = patterns(name)
        conn.execute("INSERT OR REPLACE INTO systems VALUES (?, ?)", (name, "|".join(pats)))
        if uhc:
            status = "in" if uhc.upper() == "X" else "verify"
            detail = ("listed (system level)" if status == "in"
                      else "not found as an organization in UHC's 2027 directory - verify")
            conn.execute("INSERT INTO system_network VALUES (?,?,?,?,?,?,?)",
                         (name, "UHC", status, detail, UHC_SCOPE, UHC_SOURCE, "2026-08-10"))
        homes = HOME_CITIES.get(name)
        hits = [(c, city) for c, h, city in nclin if any(p in c or p in h for p in pats)
                and (not homes or norm(city) in homes)]
        if hits:
            towns = sorted({c for _, c in hits if c})[:4]
            conn.execute("INSERT INTO system_network VALUES (?,?,?,?,?,?,?)", (
                name, "Medica", "in", f"{len(hits)} locations in the 2027 directory ({', '.join(towns)}...)",
                "Medica Advantage PPO", "Medica 2027 Provider Directory", "2027"))
        else:
            conn.execute("INSERT INTO system_network VALUES (?,?,?,?,?,?,?)", (
                name, "Medica", "verify", "not found in the Medica 2027 directory - verify",
                "Medica Advantage PPO", "Medica 2027 Provider Directory", "2027"))
    # agency confirmations (weekly lists)
    conn.execute("DROP TABLE IF EXISTS confirmations")
    conn.execute("""CREATE TABLE confirmations (provider TEXT, clinic TEXT, system TEXT, carrier TEXT, plans TEXT,
        status TEXT, confirmed_by TEXT, confirmed_on TEXT, note TEXT)""")
    if os.path.exists(CONFIRM):
        with open(CONFIRM, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                conn.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?,?)", tuple(
                    (r.get(k) or "").strip() for k in ("provider", "clinic", "system", "carrier", "plans", "status",
                                                        "confirmed_by", "confirmed_on", "note")))
    conn.commit()
    s = dict(conn.execute("SELECT carrier, COUNT(*) FROM system_network WHERE status='in' GROUP BY carrier"))
    v = dict(conn.execute("SELECT carrier, COUNT(*) FROM system_network WHERE status='verify' GROUP BY carrier"))
    print(f"systems: {len(systems)} | in network: {s} | verify: {v} | confirmations: "
          f"{conn.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]}")


if __name__ == "__main__":
    main()
