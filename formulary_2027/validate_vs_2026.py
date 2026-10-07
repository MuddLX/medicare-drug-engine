"""Compare matched 2027 lists with the same carrier's official 2026 CMS list (most drugs carry over)."""
import json, os, sqlite3, sys, collections
HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS = {"bcbs_27044": "00026288", "bcbs_27045": "00026291", "medicareblue_27147": "00026248",
         "healthpartners": "00026132", "aetna_27015": "00026013", "align": "00026213",
         "uhc_27002": "00026002", "aarp_preferred_27000": "00026000", "aarp_saver_27001": "00026001",
         "wellcare_classic_27163_2027": "00026191", "wellcare_valuescript_27165_2027": "00026195"}
db = sqlite3.connect("file:" + os.path.join(HERE, "..", "medicare_mn.db") + "?mode=ro", uri=True)
for name, fid in PAIRS.items():
    p = os.path.join(HERE, "matched", name + ".json")
    if not os.path.exists(p):
        continue
    rows = json.load(open(p))
    ours = {}
    for r in rows:
        for x in r["rxcuis"]:
            ours.setdefault(x, r["tier"])
    old = dict(db.execute("select rxcui, min(tier) from formulary where formulary_id=? group by rxcui", (fid,)))
    both = set(ours) & set(old)
    same_tier = sum(ours[x] == old[x] for x in both)
    print(f"{name:32} 2026 IDs {len(old):5} | ours {len(ours):5} | in both {len(both):5} "
          f"| 2026 found {len(both)/max(1,len(old)):.0%} | ours also in 2026 {len(both)/max(1,len(ours)):.0%} "
          f"| same tier {same_tier/max(1,len(both)):.0%}")
