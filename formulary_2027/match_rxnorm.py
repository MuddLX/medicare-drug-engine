"""Match each 2027 carrier drug-list row to RxNorm drug IDs (RxCUIs), offline.

Input : <formulary>.json files from parse_formulary_pdf.py (or Wellcare search-tool .tsv),
        rxnorm_concepts.json from download_rxnorm.py.
Output: matched/<formulary>.json   [{name, tier, limits, brand, status, rxcuis:[...]}]
        matched/summary.txt

Rule of thumb: a row only matches products that have ALL of its ingredients, the same dose form,
the same release type (ER / DR / plain) and, when strengths are printed, one of those strengths.
A brand row (ELIQUIS, Eliquis (B)) only matches that brand's products; a generic row only generics.
Nothing is guessed: a row that fits nothing is left "unmatched" for a person to check.
"""
import csv, json, os, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.dirname(HERE))
from app.rxnorm_core import (ABBR, SALTS, FORM_WORDS, NUM_UNIT, words, release_of, form_of, numbers,  # noqa: E402,F401
                             BASE_EQUIV, close, RX_COMPONENT, parse_rx, ing_key, RxIndex, candidate_brand,
                             canon_words, BRAND_GENERIC_SPLIT)


def split_row(name, bg, style):
    """-> (brand or '', drug text). Decide brand vs generic from the carrier's own convention."""
    raw = name.strip()
    raw = re.sub(r"\((generic|brand)[^)]*\)", " ", raw, flags=re.I)     # "(generic Glucophage XR)"
    parts = BRAND_GENERIC_SPLIT.split(raw, maxsplit=1)
    if len(parts) == 2 and not re.search(r"\d", parts[0]):              # "ELIQUIS - apixaban tab 5 mg"
        return parts[0].strip(), parts[1], raw
    first = re.match(r"[A-Za-z][A-Za-z0-9'\.]*(?:[ -][A-Za-z][A-Za-z0-9'\.]*){0,3}", raw)
    is_brand = False
    if style == "bg":
        is_brand = bg == "B"
    elif style == "flag":
        is_brand = bg in ("1", "B", "true")
    else:                                                              # ALL CAPS = brand
        lead = raw.split()[0] if raw.split() else ""
        is_brand = lead.isupper() and len(re.sub(r"[^A-Za-z]", "", lead)) >= 3
    return ("@" if is_brand else ""), raw, raw


def match_row(index, row, style):
    status, rx, brand = _match_row(index, row, style)
    if status != "matched" and not brand:          # branded generic printed like a generic (Camila, claravis)
        b = candidate_brand(index, split_row(row["name"], row.get("bg", ""), style)[1])
        if b:
            status, rx, brand = _match_row(index, row, style, force_brand=b)
    return status, rx, brand


def _match_row(index, row, style, force_brand=""):
    name = row["name"]
    brand_hint, text, raw = split_row(name, row.get("bg", ""), style)
    rel = release_of(text)
    form, sub = form_of(text)
    nums = numbers(text)
    for drug, eq in BASE_EQUIV.items():
        if drug in text.lower():
            nums |= {eq[n] / k for n in list(nums) if n in eq for k in (1, 1000)}
    vols = {float(v) for v in re.findall(r"/\s*(\d+(?:\.\d+)?)\s*ml\b", text.lower()) if float(v) > 0}
    tw = canon_words(index, set(words(text)))
    if force_brand:
        brand = force_brand
    elif brand_hint and brand_hint != "@":
        brand = candidate_brand(index, brand_hint)   # "glipizide xl - glipizide ..." is not a brand: match as generic
    elif brand_hint == "@":
        brand = candidate_brand(index, text)
    else:
        brand = ""
    if brand:
        pool = index.by_brand.get(brand, set())
        want_brand = True
    else:
        drug_words = [w for w in tw if w not in FORM_WORDS and w not in SALTS and len(w) > 2] or \
                     [w for w in tw if w in SALTS]                     # potassium chloride, sodium bicarbonate
        if not drug_words:
            return "unmatched", [], ""
        pool = set.intersection(*[index.by_word.get(w, set()) for w in drug_words[:1]]) if drug_words else set()
        pool = set()
        for w in drug_words:
            pool |= index.by_word.get(w, set())
        want_brand = False
    hits = []
    for i in pool:
        p = index.products[i]
        if want_brand != bool(p["brand"]):
            continue
        if not want_brand:
            if not all(k <= tw for k in p["ings"]):                       # every ingredient named
                continue
            extra = {w for w in tw if w not in FORM_WORDS and w not in SALTS and len(w) > 2
                     and w in index.by_word} - p["ing_words"]
            if extra:                                                      # row names something the product lacks
                continue
        row_salts = {w for w in tw if w in SALTS}
        if not want_brand and row_salts and not (p["salts"] <= row_salts):
            continue
        if form and p["form"][0] and form != p["form"][0]:
            continue
        if form == "tab" and p["form"][0] == "tab" and sub != p["form"][1]:
            continue
        if form == "topical" and p["form"][0] == "topical" and sub != p["form"][1]:
            continue
        if form == "inj" and sub and sub != p["form"][1]:
            continue
        if form == "inhal" and p["form"][0] == "inhal" and sub != p["form"][1]:
            continue
        if form == "ophth" and sub and p["form"][1] and sub != p["form"][1]:
            continue
        if form in ("tab", "cap", "oral_liq", "inj") and rel != p["rel"]:
            continue
        if nums and not (any if p["pack"] else all)(close(s, nums) for s in p["strengths"]):
            continue
        if vols and p["qty"] and not close(p["qty"], vols):          # 45 mg/0.5 mL syringe, not the 1 mL one
            continue
        hits.append(p)
    if not hits:
        return "unmatched", [], brand
    # a generic "pack" row should not pull every single-product SCD and vice versa
    packs = [p for p in hits if p["pack"]]
    if re.search(r"\b(pack|kit|starter|titration)\b", text, re.I) and packs:
        hits = packs
    elif len(packs) < len(hits):
        hits = [p for p in hits if not p["pack"]]
    return "matched", sorted({p["rxcui"] for p in hits}), brand


def load_rows(path):
    if path.endswith(".tsv"):
        with open(path, encoding="utf-8") as f:
            return [{"name": r["name"], "tier": int(r["tier"]), "limits": "", "bg": "1" if r.get("brand") == "1" else "0"}
                    for r in csv.DictReader(f, delimiter="\t") if r["tier"].strip().isdigit()], "flag"   # skip "Non-Formulary"
    rows = json.load(open(path, encoding="utf-8"))
    style = "bg" if sum(1 for r in rows if r.get("bg")) > len(rows) * 0.5 else "caps"
    return rows, style


def main(paths):
    concepts = json.load(open(os.path.join(HERE, "rxnorm_concepts.json"), encoding="utf-8"))["concepts"]
    index = RxIndex(concepts)
    os.makedirs(os.path.join(HERE, "matched"), exist_ok=True)
    lines = []
    for path in paths:
        rows, style = load_rows(path)
        out, n_match = [], 0
        for r in rows:
            status, rx, brand = match_row(index, r, style)
            n_match += status == "matched"
            out.append({"name": r["name"], "tier": r["tier"], "limits": r.get("limits", ""),
                        "brand": brand, "status": status, "rxcuis": rx})
        base = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(HERE, "matched", base + ".json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        n_rx = len({x for o in out for x in o["rxcuis"]})
        lines.append(f"{base:32} rows {len(rows):5}  matched {n_match:5} ({n_match / max(1, len(rows)):.0%})  drug IDs {n_rx:6}  style {style}")
        print(lines[-1])
    open(os.path.join(HERE, "matched", "summary.txt"), "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
