"""Tier grid for common Medicare drugs across every matched 2027 drug list (a sanity check people can read)."""
import csv, json, os, re, glob
HERE = os.path.dirname(os.path.abspath(__file__))
DRUGS = [  # label, regex on the RxNorm product name
 ("atorvastatin 20 mg tab", r"^atorvastatin 20 MG Oral Tablet$"), ("rosuvastatin 10 mg tab", r"^rosuvastatin calcium 10 MG Oral Tablet$"),
 ("simvastatin 20 mg tab", r"^simvastatin 20 MG Oral Tablet$"), ("lisinopril 10 mg tab", r"^lisinopril 10 MG Oral Tablet$"),
 ("losartan 50 mg tab", r"^losartan potassium 50 MG Oral Tablet$"), ("amlodipine 5 mg tab", r"^amlodipine 5 MG Oral Tablet$"),
 ("metoprolol succinate ER 25 mg", r"^24 HR metoprolol succinate 25 MG Extended Release Oral Tablet$"),
 ("metoprolol tartrate 25 mg", r"^metoprolol tartrate 25 MG Oral Tablet$"), ("carvedilol 12.5 mg", r"^carvedilol 12.5 MG Oral Tablet$"),
 ("hydrochlorothiazide 25 mg", r"^hydrochlorothiazide 25 MG Oral Tablet$"), ("furosemide 20 mg", r"^furosemide 20 MG Oral Tablet$"),
 ("metformin 500 mg", r"^metformin hydrochloride 500 MG Oral Tablet$"),
 ("metformin ER 500 mg", r"^24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet$"),
 ("glipizide 5 mg", r"^glipizide 5 MG Oral Tablet$"), ("levothyroxine 50 mcg", r"^levothyroxine sodium 0.05 MG Oral Tablet$"),
 ("omeprazole 20 mg DR cap", r"^omeprazole 20 MG Delayed Release Oral Capsule$"),
 ("pantoprazole 40 mg DR tab", r"^pantoprazole 40 MG Delayed Release Oral Tablet$"),
 ("gabapentin 300 mg cap", r"^gabapentin 300 MG Oral Capsule$"), ("sertraline 50 mg", r"^sertraline 50 MG Oral Tablet$"),
 ("escitalopram 10 mg", r"^escitalopram 10 MG Oral Tablet$"), ("trazodone 50 mg", r"^trazodone hydrochloride 50 MG Oral Tablet$"),
 ("tamsulosin 0.4 mg cap", r"^tamsulosin hydrochloride 0.4 MG Oral Capsule$"), ("montelukast 10 mg", r"^montelukast 10 MG Oral Tablet$"),
 ("clopidogrel 75 mg", r"^clopidogrel 75 MG Oral Tablet$"), ("donepezil 10 mg", r"^donepezil hydrochloride 10 MG Oral Tablet$"),
 ("warfarin 5 mg", r"^warfarin sodium 5 MG Oral Tablet$"), ("prednisone 10 mg", r"^prednisone 10 MG Oral Tablet$"),
 ("sitagliptin 100 mg (generic)", r"^sitagliptin (phosphate )?100 MG Oral Tablet$"),
 ("Eliquis 5 mg", r"^apixaban 5 MG Oral Tablet \[Eliquis\]$"), ("Xarelto 20 mg", r"^rivaroxaban 20 MG Oral Tablet \[Xarelto\]$"),
 ("Jardiance 10 mg", r"^empagliflozin 10 MG Oral Tablet \[Jardiance\]$"), ("Farxiga 10 mg", r"^dapagliflozin 10 MG Oral Tablet \[Farxiga\]$"),
 ("Januvia 100 mg", r"^sitagliptin phosphate 100 MG Oral Tablet \[Januvia\]$"),
 ("Ozempic pen", r"semaglutide.*Pen Injector \[Ozempic\]$"), ("Trulicity pen", r"dulaglutide.*\[Trulicity\]$"),
 ("Mounjaro pen", r"tirzepatide.*\[Mounjaro\]$"), ("Lantus SoloStar", r"insulin glargine.*Pen Injector \[Lantus\]$"),
 ("Entresto 24-26 mg", r"^sacubitril 24 MG / valsartan 26 MG Oral Tablet \[Entresto\]$"),
 ("Trelegy Ellipta", r"\[Trelegy\]$"), ("Breo Ellipta", r"\[Breo\]$"), ("Symbicort", r"\[Symbicort\]$"),
 ("Spiriva Respimat", r"tiotropium.*Inhalation Spray \[Spiriva\]$"), ("albuterol HFA", r"albuterol 0.09 MG/ACTUAT Metered Dose Inhaler$"),
]
FILES = sorted(glob.glob(os.path.join(HERE, "matched", "*.json")))
concepts = json.load(open(os.path.join(HERE, "rxnorm_concepts.json"), encoding="utf-8"))["concepts"]
cols, grid = [], {d[0]: {} for d in DRUGS}
for f in FILES:
    name = os.path.splitext(os.path.basename(f))[0]
    if name == "wellcare_valuescript":            # PDF read superseded by the search-tool list
        continue
    cols.append(name)
    tier = {}
    for row in json.load(open(f, encoding="utf-8")):
        for rx in row["rxcuis"]:
            tier[rx] = min(tier.get(rx, 9), row["tier"])
    for label, pat in DRUGS:
        ids = [c["rxcui"] for c in concepts if c["tty"] in ("SCD", "SBD") and re.search(pat, c["name"])]
        tiers = [tier[i] for i in ids if i in tier]
        grid[label][name] = (f"T{min(tiers)}" if tiers else "-") if ids else "?"
with open(os.path.join(HERE, "matched", "common_drug_grid.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Drug"] + cols)
    for label, _ in DRUGS:
        w.writerow([label] + [grid[label][c] for c in cols])
print("Drug".ljust(30) + " ".join(c[:9].ljust(9) for c in cols))
for label, _ in DRUGS:
    print(label[:30].ljust(30) + " ".join(grid[label][c].ljust(9) for c in cols))
