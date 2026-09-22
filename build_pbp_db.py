"""
build_pbp_db.py — builds pbp_benefits.db from the CMS PBP Benefits public files.

Pulls the benefit fields the client-facing plan-comparison sheet needs, for the MN
plans in medicare_mn.db, keyed by (contract_id, plan_id). Re-run against the 2027
PBP files (same names) to refresh — the "drop-in" swap.

Fields are pulled BY COLUMN NAME (each file's header row is read first), so this
survives CMS's periodic column re-ordering / length changes between plan years.

Wired so far: Section D (plan-level financials), B7 (PCP + specialist),
B4 (emergency room), B1a (inpatient hospital per-day).
"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
PBP_DIR = os.path.join(HERE, "PBP file")
MN_DB = os.path.join(HERE, "medicare_mn.db")
OUT_DB = os.path.join(HERE, "pbp_benefits.db")


def mn_plan_keys():
    conn = sqlite3.connect(MN_DB)
    rows = conn.execute("SELECT DISTINCT contract_id, plan_id FROM plans").fetchall()
    conn.close()
    return {(str(cid).strip(), str(pid).strip().zfill(3)) for cid, pid in rows}


def load_pbp(path, fields, mn_keys):
    """Return {(contract_id, plan_id): {field: raw_value}} for MN plans in this file."""
    out = {}
    with open(path, encoding="latin-1") as f:
        header = f.readline().rstrip("\r\n").split("\t")
        idx = {name: i for i, name in enumerate(header)}
        missing = [x for x in fields + ["pbp_a_hnumber", "pbp_a_plan_identifier"] if x not in idx]
        if missing:
            raise KeyError(f"{os.path.basename(path)}: missing columns {missing}")
        h_i, p_i = idx["pbp_a_hnumber"], idx["pbp_a_plan_identifier"]
        need = max(idx[x] for x in fields + ["pbp_a_hnumber", "pbp_a_plan_identifier"])
        for line in f:
            cols = line.rstrip("\r\n").split("\t")
            if len(cols) < len(header):
                cols += [""] * (len(header) - len(cols))
            if len(cols) <= need:
                continue
            key = (cols[h_i].strip(), cols[p_i].strip().zfill(3))
            if key not in mn_keys:
                continue
            out[key] = {fld: cols[idx[fld]].strip() for fld in fields}
    return out


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def yes(v):
    return str(v).strip() == "1"          # CMS: 1 = Yes, 2 = No


def visit_share(r, copay_yn, copay_amt, coins_yn, coins_pct):
    """Cost-share for a covered medical visit: copay ($), coinsurance (%), or $0 if neither."""
    if yes(r[copay_yn]):
        a = num(r[copay_amt])
        return (a if a is not None else 0.0, None)
    if yes(r[coins_yn]):
        return (None, num(r[coins_pct]))
    return (0.0, None)   # covered, no copay & no coinsurance = $0


def deduct(r):
    """Medical deductible: annual, falling back to in-network then combined."""
    for yn, amt in [("pbp_d_ann_deduct_yn", "pbp_d_ann_deduct_amt"),
                    ("pbp_d_inn_deduct_yn", "pbp_d_inn_deduct_amt"),
                    ("pbp_d_comb_deduct_yn", "pbp_d_comb_deduct_amt")]:
        if yes(r[yn]):
            v = num(r[amt])
            if v is not None:
                return v
    return 0.0


def build_section_d(mn):
    fields = [
        "pbp_d_out_pocket_amt_yn", "pbp_d_out_pocket_amt",
        "pbp_d_comb_max_enr_amt_yn", "pbp_d_comb_max_enr_amt",
        "pbp_d_ann_deduct_yn", "pbp_d_ann_deduct_amt",
        "pbp_d_inn_deduct_yn", "pbp_d_inn_deduct_amt",
        "pbp_d_comb_deduct_yn", "pbp_d_comb_deduct_amt",
        "pbp_d_mco_pay_reduct_yn", "pbp_d_mco_pay_reduct_amt",
        "pbp_d_mplusc_premium",
    ]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_Section_D.txt"), fields, mn)
    out = {}
    for key, r in raw.items():
        out[key] = {
            "oop_max_inn": num(r["pbp_d_out_pocket_amt"]) if yes(r["pbp_d_out_pocket_amt_yn"]) else None,
            "oop_max_comb": num(r["pbp_d_comb_max_enr_amt"]) if yes(r["pbp_d_comb_max_enr_amt_yn"]) else None,
            "medical_deductible": deduct(r),
            "part_b_giveback": num(r["pbp_d_mco_pay_reduct_amt"]) if yes(r["pbp_d_mco_pay_reduct_yn"]) else None,
            "premium_pbp": num(r["pbp_d_mplusc_premium"]),
        }
    return out


def build_b7(mn):
    """B7a = Primary Care Physician, B7d = Physician Specialist. Copay OR coinsurance."""
    fields = [
        "pbp_b7a_copay_yn", "pbp_b7a_copay_amt_mc_min", "pbp_b7a_coins_yn", "pbp_b7a_coins_pct_mc_min",
        "pbp_b7d_copay_yn", "pbp_b7d_copay_amt_mc_min", "pbp_b7d_coins_yn", "pbp_b7d_coins_pct_mc_min",
    ]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b7_health_prof.txt"), fields, mn)
    out = {}
    for key, r in raw.items():
        pcp_copay, pcp_coins = visit_share(r, "pbp_b7a_copay_yn", "pbp_b7a_copay_amt_mc_min",
                                           "pbp_b7a_coins_yn", "pbp_b7a_coins_pct_mc_min")
        spec_copay, spec_coins = visit_share(r, "pbp_b7d_copay_yn", "pbp_b7d_copay_amt_mc_min",
                                             "pbp_b7d_coins_yn", "pbp_b7d_coins_pct_mc_min")
        out[key] = {"pcp_copay": pcp_copay, "pcp_coins": pcp_coins,
                    "spec_copay": spec_copay, "spec_coins": spec_coins}
    return out


def build_b4(mn):
    """B4a = Emergency care. Same copay/coinsurance shape as a visit."""
    fields = ["pbp_b4a_copay_yn", "pbp_b4a_copay_amt_mc_min",
              "pbp_b4a_coins_yn", "pbp_b4a_coins_pct_mc_min"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b4_emerg_urgent.txt"), fields, mn)
    out = {}
    for key, r in raw.items():
        er_copay, er_coins = visit_share(r, "pbp_b4a_copay_yn", "pbp_b4a_copay_amt_mc_min",
                                         "pbp_b4a_coins_yn", "pbp_b4a_coins_pct_mc_min")
        out[key] = {"er_copay": er_copay, "er_coins": er_coins}
    return out


def build_b1a(mn):
    """B1a = Inpatient hospital (acute). Per-day copay across a day interval
    (e.g. $275/day, days 1-7), a flat per-stay copay, or coinsurance. Tier 1."""
    fields = ["pbp_b1a_copay_yn", "pbp_b1a_copay_mcs_amt_int1_t1",
              "pbp_b1a_copay_mcs_bgnd_int1_t1", "pbp_b1a_copay_mcs_endd_int1_t1",
              "pbp_b1a_copay_mcs_amt_t1",
              "pbp_b1a_coins_yn", "pbp_b1a_coins_mcs_pct_t1"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b1a_inpat_hosp.txt"), fields, mn)
    out = {}
    for key, r in raw.items():
        copay = coins = day_b = day_e = None
        if yes(r["pbp_b1a_copay_yn"]):
            amt_int1 = num(r["pbp_b1a_copay_mcs_amt_int1_t1"])
            if amt_int1 is not None:                       # per-day copay over an interval
                copay = amt_int1
                day_b = num(r["pbp_b1a_copay_mcs_bgnd_int1_t1"])
                day_e = num(r["pbp_b1a_copay_mcs_endd_int1_t1"])
            else:                                          # flat per-stay copay
                flat = num(r["pbp_b1a_copay_mcs_amt_t1"])
                copay = flat if flat is not None else 0.0
        elif yes(r["pbp_b1a_coins_yn"]):
            coins = num(r["pbp_b1a_coins_mcs_pct_t1"])
        else:
            copay = 0.0
        out[key] = {"hosp_copay": copay, "hosp_coins": coins,
                    "hosp_day_begin": day_b, "hosp_day_end": day_e}
    return out


def build_b16(mn):
    """B16 dental. Client 'dental allowance' = comprehensive annual max plan
    benefit, falling back to the preventive allowance."""
    fields = ["pbp_b16c_maxplan_cmp_yn", "pbp_b16c_maxplan_cmp_amt",
              "pbp_b16b_maxplan_pv_yn", "pbp_b16b_maxplan_pv_amt"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b16_dental.txt"), fields, mn)
    out = {}
    for key, r in raw.items():
        allow = None
        if yes(r["pbp_b16c_maxplan_cmp_yn"]):
            allow = num(r["pbp_b16c_maxplan_cmp_amt"])
        if allow is None and yes(r["pbp_b16b_maxplan_pv_yn"]):
            allow = num(r["pbp_b16b_maxplan_pv_amt"])
        out[key] = {"dental_allowance": allow}
    return out


def maxplan_allow(r, yn, amt):
    """Annual dollar allowance = max plan benefit amount, gated by its yes/no flag."""
    return num(r[amt]) if yes(r[yn]) else None


def build_b17(mn):
    """B17b = eye wear. Vision allowance = combined eyewear max plan benefit."""
    fields = ["pbp_b17b_comb_maxplan_yn", "pbp_b17b_comb_maxplan_amt"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b17_eye_exams_wear.txt"), fields, mn)
    return {k: {"vision_allowance": maxplan_allow(r, "pbp_b17b_comb_maxplan_yn",
            "pbp_b17b_comb_maxplan_amt")} for k, r in raw.items()}


def build_b18(mn):
    """B18b = hearing aids. Hearing allowance = hearing-aid max plan benefit."""
    fields = ["pbp_b18b_maxplan_yn", "pbp_b18b_maxplan_amt"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b18_hearing_exams_aids.txt"), fields, mn)
    return {k: {"hearing_allowance": maxplan_allow(r, "pbp_b18b_maxplan_yn",
            "pbp_b18b_maxplan_amt")} for k, r in raw.items()}


def build_b13(mn):
    """B13b = OTC. OTC allowance = OTC max plan benefit (dollar amount)."""
    fields = ["pbp_b13b_maxplan_yn", "pbp_b13b_maxplan_amt"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b13_other_services.txt"), fields, mn)
    return {k: {"otc_amount": maxplan_allow(r, "pbp_b13b_maxplan_yn",
            "pbp_b13b_maxplan_amt")} for k, r in raw.items()}


def build_b14(mn):
    """B14c 'mhc' = Fitness Benefit (labels: '14C Fitness Ben Type' / 'FB Ben Desc').
    A yes/no: offered if the fitness flag is set or a fitness type is named."""
    fields = ["pbp_b14c_bendesc_amo_mhc", "pbp_b14c_bendesc_typ_mhc"]
    raw = load_pbp(os.path.join(PBP_DIR, "pbp_b14_preventive.txt"), fields, mn)
    out = {}
    for k, r in raw.items():
        offered = yes(r["pbp_b14c_bendesc_amo_mhc"]) or bool(r["pbp_b14c_bendesc_typ_mhc"].strip())
        out[k] = {"fitness_included": 1.0 if offered else None}
    return out


COLUMNS = ["oop_max_inn", "oop_max_comb", "medical_deductible", "part_b_giveback",
           "premium_pbp", "pcp_copay", "pcp_coins", "spec_copay", "spec_coins",
           "er_copay", "er_coins", "hosp_copay", "hosp_coins", "hosp_day_begin", "hosp_day_end",
           "dental_allowance", "vision_allowance", "hearing_allowance", "otc_amount",
           "fitness_included"]


def main():
    mn = mn_plan_keys()
    print(f"MN plans in medicare_mn.db: {len(mn)}")
    secd = build_section_d(mn)
    b7 = build_b7(mn)
    b4 = build_b4(mn)
    b1a = build_b1a(mn)
    b16 = build_b16(mn)
    b17 = build_b17(mn)
    b18 = build_b18(mn)
    b13 = build_b13(mn)
    b14 = build_b14(mn)
    print(f"Section D {len(secd)} | B7 {len(b7)} | B4 {len(b4)} | B1a {len(b1a)} | B16 {len(b16)} | B17 {len(b17)} | B18 {len(b18)} | B13 {len(b13)} | B14 {len(b14)}")

    benefits = {}
    for key, d in secd.items():
        p = b7.get(key, {})
        row = dict(d)
        row.update({k: p.get(k) for k in ("pcp_copay", "pcp_coins", "spec_copay", "spec_coins")})
        for src_map in (b4, b1a, b16, b17, b18, b13, b14):
            row.update(src_map.get(key, {}))
        benefits[key] = row

    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)
    conn = sqlite3.connect(OUT_DB)
    cols_sql = ", ".join(f"{c} REAL" for c in COLUMNS)
    conn.execute(f"""CREATE TABLE plan_benefits (
        contract_id TEXT, plan_id TEXT, {cols_sql},
        PRIMARY KEY (contract_id, plan_id))""")
    ph = ", ".join(["?"] * (2 + len(COLUMNS)))
    for (cid, pid), b in benefits.items():
        conn.execute(f"INSERT OR REPLACE INTO plan_benefits VALUES ({ph})",
                     (cid, pid, *[b.get(c) for c in COLUMNS]))
    conn.commit()

    def sh(copay, coins):
        return f"${copay:.0f}" if copay is not None else (f"{coins:.0f}%" if coins is not None else "?")
    print("\nSample MN plans (sanity-check these):")
    def dollar(v):
        return f"${v:.0f}" if v is not None else "none/?"
    for row in conn.execute("""SELECT contract_id, plan_id, dental_allowance,
        vision_allowance, hearing_allowance, otc_amount, fitness_included
        FROM plan_benefits ORDER BY contract_id, plan_id LIMIT 30"""):
        cid, pid, dent, vis, hear, otc, fitn = row
        fitstr = "yes" if fitn == 1 else "no"
        print(f"  {cid} {pid}  dental={dollar(dent)}  vision={dollar(vis)}  "
              f"hearing={dollar(hear)}  OTC={dollar(otc)}  fitness={fitstr}")
    fit = conn.execute("SELECT COUNT(*) FROM plan_benefits WHERE fitness_included=1").fetchone()[0]
    tot = conn.execute("SELECT COUNT(*) FROM plan_benefits").fetchone()[0]
    print(f"\nFitness benefit: {fit} of {tot} plans include it")

    unmatched = sorted(mn - set(benefits.keys()))
    print(f"\nUnmatched MN plans ({len(unmatched)}) — no benefit row:")
    for cid, pid in unmatched:
        kind = "PDP (drug-only — never on client sheet)" if cid.startswith("S") else "MA/Cost — INVESTIGATE"
        print(f"  {cid} {pid}  {kind}")
    conn.close()
    print(f"\nWrote {OUT_DB} — {len(benefits)} plan rows.")


if __name__ == "__main__":
    main()
