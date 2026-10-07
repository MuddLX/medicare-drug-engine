"""Doctor / clinic network check for the 2027 plan year (2026-10-06, Jordon).

Only 2027 sources (providers_2027.db, built by providers_2027/*.py):
  * confirmations   - weekly lists from the agency (Jill / Lacey): a person checked this doctor/clinic.
  * directory       - full carrier directories read from their 2027 PDFs (Medica Advantage PPO so far).
  * system_network  - health-system level: "Allina is in UHC's 2027 network" (UHC from the agency
                      sheet's 2027 column; Medica derived from its 2027 directory).
Order per doctor and plan: confirmation > doctor in a 2027 directory > health system > not checked.
Never says "out of network" unless the agency confirmed it; a miss is "not found - verify".

Result per doctor: {"raw_text", "first_name", "last_name", "specialty", "clinic_name", "city",
                    "system": <health system if known>, "plans": {plan label: {"status", "detail", "accepting"}}}
status: "in" (doctor confirmed / in directory), "system" (health system in network - confirm the doctor),
        "out" (agency confirmed out), "not_found" (not in that carrier's 2027 directory), "verify"
        (system not found in the 2027 source), "not_checked" (no 2027 source for this plan yet).
"""
import os
import re
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "providers_2027.db")

# Which 2027 source covers which plans. Contract -> (carrier name in the sources, plan IDs or None = all).
DIRECTORY_SCOPE = {"H8889": ("Medica", None)}                       # Medica Advantage PPO directory
SYSTEM_SCOPE = {"H2001": ("UHC", {"116", "117", "118"}), "H8889": ("Medica", None)}
CARRIER_OF = {"H2001": "UHC", "S5921": "UHC", "H8889": "Medica", "H2450": "Medica", "H3219": "Aetna",
              "H5959": "BCBS", "H2461": "BCBS", "S5743": "BCBS", "H4882": "HealthPartners", "H3186": "Align",
              "H9834": "Quartz", "S4802": "Wellcare", "S5884": "Humana", "S5715": "HealthSpring"}


def _norm(t):
    t = (t or "").lower().replace(".", " ").replace("'", "").replace("’", "")
    t = re.sub(r"\bsaint\b", "st", t)
    return " ".join(re.sub(r"[^a-z0-9& ]", " ", t).split())


def _connect(path=None):
    path = path or DB_PATH
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _systems(conn):
    return [(name, [p for p in pats.split("|") if p]) for name, pats in conn.execute("SELECT system, patterns FROM systems")]


def _system_of_text(text, systems):
    t = _norm(text)
    if not t:
        return None
    best = None
    for name, pats in systems:
        for p in pats:
            if p and re.search(rf"(^| ){re.escape(p)}( |$)", t) and (best is None or len(p) > best[1]):
                best = (name, len(p))
    return best[0] if best else None


def _directory_match(conn, carrier, doc):
    last = (doc.get("last_name") or "").strip()
    if not last or "?" in last or "..." in last:
        return None
    rows = conn.execute("""SELECT last_name, first_name, credentials, accepting, clinic_name, city, county,
                                  health_system, specialty
                           FROM directory WHERE carrier=? AND last_name=? COLLATE NOCASE""", (carrier, last)).fetchall()
    first = (doc.get("first_name") or "").strip().lower()
    if first:
        rows = [r for r in rows if (r[1] or "").lower()[:1] == first[:1]]
    if not rows:
        return None
    want_city = _norm(doc.get("city")) or _norm(doc.get("clinic_name"))
    rows.sort(key=lambda r: (not (len(first) > 1 and (r[1] or "").lower().startswith(first[:3])),
                             not (want_city and (_norm(r[5]) in want_city or want_city in _norm(r[4]))),
                             r[3] != "Y"))
    return rows[0]


def _confirmation(conn, doc, carrier, plan_id, system):
    last = _norm(doc.get("last_name"))
    clinic = _norm(doc.get("clinic_name"))
    for prov, cl, sysn, car, plans, status, who, when, note in conn.execute("SELECT * FROM confirmations"):
        if _norm(car) not in (_norm(carrier), "all"):
            continue
        if plans.strip() and plans.strip().lower() != "all" and plan_id not in re.findall(r"\d{3}", plans):
            continue
        hit = (last and last in _norm(prov).split()) or (clinic and _norm(cl) and _norm(cl) in clinic) or \
              (system and _norm(sysn) == _norm(system))
        if hit:
            st = "out" if status.strip().lower().startswith("out") else "in"
            return st, f"confirmed by {who or 'agency'} {when}".strip()
    return None


def short_system(name):
    """'MHealth Fairview (including Healtheast rebranded)' -> 'MHealth Fairview'."""
    return re.sub(r"\s*\(.*?\)", "", name or "").strip()


def check(providers, plans, db_path=None):
    """providers: the sheet's doctor list. plans: [(label, contract_id, plan_id, plan_type)]."""
    conn = _connect(db_path)
    out = []
    systems = _systems(conn) if conn else []
    for doc in providers or []:
        entry = {k: (doc.get(k) or "") for k in ("raw_text", "first_name", "last_name", "specialty", "clinic_name", "city")}
        entry["plans"] = {}
        written = " ".join([doc.get("clinic_name") or "", doc.get("raw_text") or ""])
        system = _system_of_text(written, systems) if conn else None
        dir_hits = {}
        if conn:
            for cid, (carrier, _) in DIRECTORY_SCOPE.items():
                m = _directory_match(conn, carrier, doc)
                if m:
                    dir_hits[cid] = m
                    system = system or _system_of_text(f"{m[4]} {m[7]}", systems)
        entry["system"] = short_system(system)
        for label, cid, pid, ptype in plans:
            if ptype == "PD":
                continue
            res = {"status": "not_checked", "detail": "no 2027 source yet", "accepting": ""}
            if conn:
                carrier = CARRIER_OF.get(cid, "")
                conf = _confirmation(conn, doc, carrier, pid, system)
                d_scope = DIRECTORY_SCOPE.get(cid)
                s_scope = SYSTEM_SCOPE.get(cid)
                if conf:
                    res = {"status": conf[0], "detail": conf[1], "accepting": ""}
                elif d_scope and cid in dir_hits:
                    m = dir_hits[cid]
                    res = {"status": "in", "detail": f"{m[1]} {m[0]}, {m[2]} · {m[4].title()[:28]} · {m[5]}",
                           "accepting": "N" if m[3] == "N" else ""}
                elif d_scope and (doc.get("last_name") or "").strip():
                    # the carrier's full directory is loaded and this doctor isn't in it
                    res = {"status": "not_found", "detail": "not in 2027 directory", "accepting": ""}
                    if system:
                        row = conn.execute("SELECT status FROM system_network WHERE system=? AND carrier=?",
                                           (system, d_scope[0])).fetchone()
                        if row and row[0] == "in":
                            res["detail"] = f"{short_system(system)} in; doctor not listed"
                elif s_scope and (s_scope[1] is None or pid in s_scope[1]) and system:
                    row = conn.execute("SELECT status, detail FROM system_network WHERE system=? AND carrier=?",
                                       (system, s_scope[0])).fetchone()
                    if row and row[0] == "in":
                        res = {"status": "system", "detail": f"{short_system(system)} · system level", "accepting": ""}
                    else:
                        res = {"status": "verify", "detail": f"{short_system(system)} · not listed",
                               "accepting": ""}
                elif s_scope and (s_scope[1] is None or pid in s_scope[1]):
                    res = {"status": "verify", "detail": "clinic not recognised", "accepting": ""}
            entry["plans"][label] = res
        out.append(entry)
    if conn:
        conn.close()
    return out
