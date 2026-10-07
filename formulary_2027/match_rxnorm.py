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

ABBR = {"hcl": "hydrochloride", "hbr": "hydrobromide", "na": "sodium", "k": "potassium", "mag": "magnesium",
        "succ": "succinate", "tart": "tartrate", "ace": "acetate", "phos": "phosphate", "sulf": "sulfate",
        "bitart": "bitartrate", "dipropionate": "dipropionate", "w": "", "acetonide": "acetonide",
        "estradiol": "estradiol", "eth": "ethinyl", "ethin": "ethinyl", "levonorg": "levonorgestrel"}
SALTS = set("""hydrochloride dihydrochloride hydrobromide sodium potassium calcium magnesium besylate maleate succinate
tartrate bitartrate mesylate dimesylate fumarate hemifumarate sulfate phosphate acetate citrate bromide chloride hyclate
monohydrate dihydrate trihydrate hemihydrate anhydrous free base equiv equivalent dipropionate propionate valerate
butyrate disodium dipotassium lactate gluconate carbonate oxide hydroxide nitrate tosylate besilate
mononitrate dinitrate aspartate saccharate monosodium trisodium stearate palmitate pamoate decanoate enanthate
cypionate undecanoate furoate xinafoate hemitartrate erbumine olamine meglumine tromethamine arginine lysine
salicylate benzoate methylsulfate methylbromide iodide fluoride polistirex ditartrate bisulfate ascorbate
hippurate glycinate medoxomil axetil pivoxil cilexetil proxetil etabonate monohydrochloride hydrate""".split())
FORM_WORDS = set("""oral tab tabs tablet tablets cap caps capsule capsules soln solution susp suspension syrup elixir liquid
conc concentrate inj injection injectable intravenous subcutaneous intramuscular iv im sc subq pen pens injector auto
autoinjector syringe syringes prefilled pfs vial vials cartridge kit pack packs therapy starter titration cream crm
ointment oint gel lotion foam shampoo patch patches ptch ptwk transdermal td external topical ophthalmic ophth eye otic
ear nasal spray inhal inhalation inhaler aero aerosol hfa nebu neb nebulization powder recon reconstituted for chew
chewable odt disintegrating dispersible sl sublingual buccal film films er xr xl sr cr la dr ec delayed extended release
released hr hour hours day days rectal suppository supp enema vaginal vag insert ring granules packet packets piggyback
pf preservative free single dose multi use emulsion drops drop mouthwash rinse paste dental implant intrauterine
iud system diskus respimat ellipta actuat act actuation metered mdi dpi inh in and with or of the per each mg mcg ml gm
gram grams g unit units unt meq mmol iu percent hcl base equiv equivalent plain generic brand cfc free ped pediatric
adult tube kwikpen flexpen solostar flextouch tempo pen-injector auto-injector prefilled""".split())
BRAND_GENERIC_SPLIT = re.compile(r"\s+-\s+")

NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)*)\s*(mg|mcg|gm|g|ml|unit|units|unt|meq|mmol|iu|%|"
                      r"million|billion)?(?:\s*/\s*(\d+(?:\.\d+)?)?\s*(ml|mg|gm|g|act|actuat|hr|day|spray|dose)?)?", re.I)


def words(text):
    text = text.lower().replace("&", " ").replace("/", " ").replace("-", " ")
    out = []
    for w in re.findall(r"[a-z][a-z']*", text):
        w = ABBR.get(w, w)
        if w:
            out.append(w)
    return out


def release_of(text):
    t = " " + re.sub(r"/\s*(24|12)?\s*(hr|hrs|hour|hours|day)\b", " ", text.lower()) + " "
    if re.search(r"\b(er|xr|xl|sr|cr|la|xr\d+|extended|ext|12\s?hr|24\s?hr|12 hour|24 hour|osm)\b|\bhr\b", t):
        return "er"
    if re.search(r"\b(dr|ec|delayed|enteric)\b", t):
        return "dr"
    return "ir"


def form_of(text):
    """(class, subtype) of a dose form, from carrier text or an RxNorm form string."""
    t = " " + text.lower().replace("-", " ") + " "
    has = lambda pat: re.search(pat, t) is not None
    if has(r"\b(inj|injection|injectable|syringe|pen|pens|injector|autoinjector|cartridge|vial|intravenous|"
           r"subcutaneous|intramuscular|im|iv|sc|subq|kwikpen|flexpen|solostar|flextouch|piggyback|infusion|prefilled)\b"):
        sub = "pen" if has(r"\b(pen|pens|injector|autoinjector|kwikpen|flexpen|solostar|flextouch)\b") else \
              "syringe" if has(r"\b(syringe|prefilled|pfs)\b") else ""
        return "inj", sub
    if has(r"\b(ophth|ophthalmic|eye)\b"):
        for sub, pat in (("susp", r"\b(susp|suspension)\b"), ("oint", r"\b(oint|ointment)\b"), ("gel", r"\bgel\b"),
                         ("emul", r"\bemulsion\b"), ("soln", r"\b(soln|solution|drops)\b")):
            if has(pat):
                return "ophth", sub
        return "ophth", ""
    if has(r"\b(otic|ear)\b"): return "otic", ""
    if has(r"\bnasal\b"): return "nasal", ""
    if has(r"\b(inhal|inhalation|inhaler|aero|aerosol|hfa|nebu|neb|nebulization|diskus|respimat|ellipta|"
           r"actuat|metered|mdi|dpi|inh)\b"):
        if has(r"\b(nebu|neb|nebulization|nebulizer)\b") or (has(r"\b(soln|solution|susp|suspension)\b") and not has(r"\b(aero|aerosol|hfa|metered|actuat|mdi|act)\b")):
            return "inhal", "neb"
        if has(r"\b(powder|dpi|diskus|ellipta|breath|capsule|cap)\b"):
            return "inhal", "powder"
        return "inhal", "aerosol"
    if has(r"\b(rectal|suppository|supp|enema)\b"): return "rectal", ""
    if has(r"\b(vaginal|vag|ring|insert)\b") and not has(r"\b(tab|tablet)\b"): return "vaginal", ""
    if has(r"\b(patch|patches|ptch|ptwk|transdermal|td)\b"): return "patch", ""
    for sub in ("cream", "ointment", "gel", "lotion", "foam", "shampoo"):
        if has(rf"\b{sub}\b") or (sub == "ointment" and has(r"\boint\b")) or (sub == "cream" and has(r"\bcrm\b")):
            return "topical", sub
    if has(r"\b(external|topical|medicated pad|swab)\b"): return "topical", "other"
    if has(r"\bfilm\b"): return "film", ""
    if has(r"\b(tab|tabs|tablet|tablets|caplet)\b"):
        sub = "chew" if has(r"\b(chew|chewable)\b") else "odt" if has(r"\b(odt|disintegrating|dispersible)\b") else \
              "sl" if has(r"\b(sl|sublingual|buccal)\b") else "efferv" if has(r"\beffervescent\b") else ""
        if has(r"\bfor (oral )?suspension\b") or has(r"\bfor susp\b"):
            sub = "forsusp"
        return "tab", sub
    if has(r"\b(cap|caps|capsule|capsules)\b"): return "cap", ""
    if has(r"\b(soln|solution|susp|suspension|syrup|elixir|liquid|conc|concentrate|granules|packet|powder|"
           r"recon|emulsion|drops|mouthwash|rinse|paste|lozenge|troche|gum)\b"):
        return "oral_liq", ""
    return None, ""


def numbers(text):
    """Every strength number printed in the text, with mg/mcg/g and per-ml variants (as floats)."""
    out = set()
    t = re.sub(r"(?<![/\d])\b\d+\s*-?\s*(hour|hours|hr|hrs|day|days|week|weeks|month|months)\b", " ", text.lower())
    t = re.sub(r"\b1:\d+\b", " ", t).replace(",", " , ")
    for m in NUM_UNIT.finditer(t):
        nums = [float(x) for x in re.split(r"\s*-\s*", m.group(1)) if x]
        unit = (m.group(2) or "").lower()
        per_n = float(m.group(3)) if m.group(3) else None
        per_u = (m.group(4) or "").lower()
        for n in nums:
            out.add(n)
            if unit in ("mcg",):
                out.add(n / 1000)
            if unit in ("mg",):
                out.add(n * 1000)
            if unit in ("g", "gm"):
                out.add(n * 1000)
            if unit == "%":
                out.add(n * 10)                      # 1% = 10 MG/ML (or MG/G)
                out.add(n / 100)                     # 1% = 0.01 MG/MG
            if not per_n and per_u in ("g", "gm"):
                out.add(n / 1000)                    # 100000 unit/gm = 100 UNT/MG
            if per_n and per_n != 0:
                out.add(n / per_n)
                if unit == "mcg":
                    out.add(n / per_n / 1000)
                if unit in ("g", "gm"):
                    out.add(n * 1000 / per_n)
    return out


BASE_EQUIV = {"albuterol": {108: 90}, "levalbuterol": {59: 45}}   # salt strength printed, RxNorm uses base


def close(a, pool):
    return any(abs(a - b) <= 0.015 * max(a, b) + 1e-9 for b in pool)


RX_COMPONENT = re.compile(r"(?:(?P<qty>\d+(?:\.\d+)?) (?:ML|MG|ACTUAT|G|HR) )?(?P<ing>[a-z][^\[\]{}]*?) "
                          r"(?P<str>\d+(?:\.\d+)?) (?P<unit>[A-Z][A-Z]*(?:/[A-Z0-9.]+)?)")


def parse_rx(name):
    """RxNorm SCD/SBD name -> ingredient word-sets, strengths, form text, brand."""
    brand = ""
    m = re.search(r"\[([^\]]+)\]\s*$", name)
    if m:
        brand = m.group(1)
        name = name[:m.start()].strip()
    body = re.sub(r"^\d+ HR ", "", name)
    comps = [c.strip() for c in body.split(" / ")]
    ings, strengths = [], []
    last_tail = ""
    qm = re.match(r"^(\d+(?:\.\d+)?) ML ", body)
    qty = float(qm.group(1)) if qm else None
    for c in comps:
        mm = None
        for mm in RX_COMPONENT.finditer(c):
            pass
        if not mm:
            return None
        ings.append(mm.group("ing"))
        strengths.append(float(mm.group("str")))
        last_tail = c[mm.end():]
    return {"ings": ings, "strengths": strengths, "form": last_tail.strip(), "brand": brand, "qty": qty,
            "er": bool(re.match(r"^\d+ HR ", name)) or "Extended Release" in name}


def ing_key(ing):
    ws = [w for w in words(ing) if w not in SALTS]
    return frozenset(ws or words(ing))


class RxIndex:
    def __init__(self, concepts):
        self.products = []
        self.by_word = defaultdict(set)          # base ingredient word -> product idx (generic + brand)
        self.by_brand = defaultdict(set)         # lower brand -> product idx
        self.brands = set()
        for c in concepts:
            if c["tty"] == "BN":
                self.brands.add(c["name"].lower())
        for c in concepts:
            tty = c["tty"]
            if tty in ("SCD", "SBD"):
                p = parse_rx(c["name"])
                if not p:
                    continue
                items = [p]
            elif tty in ("GPCK", "BPCK"):
                inner = re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", c["name"])
                items = [parse_rx(re.sub(r"^\d+(?:\.\d+)? ML ", "", x)) for x in inner]
                items = [x for x in items if x and not any("inert" in i for i in x["ings"])]
                if not items:
                    continue
                bm = re.search(r"\[([^\]]+)\]\s*$", c["name"])
                for x in items:
                    x["brand"] = bm.group(1) if bm else ""
            else:
                continue
            prod = {"rxcui": c["rxcui"], "name": c["name"], "tty": tty,
                    "ings": [ing_key(i) for x in items for i in x["ings"]],
                    "ing_words": set(w for x in items for i in x["ings"] for w in words(i)),
                    "salts": set(w for x in items for i in x["ings"] for w in words(i) if w in SALTS),
                    "strengths": [s for x in items for s in x["strengths"]],
                    "form": form_of(items[0]["form"]),
                    "rel": "er" if any(x["er"] for x in items) else
                           ("dr" if any("Delayed Release" in x["form"] for x in items) else "ir"),
                    "brand": items[0]["brand"].lower(), "pack": tty in ("GPCK", "BPCK"),
                    "qty": items[0].get("qty") if tty in ("SCD", "SBD") else None}
            idx = len(self.products)
            self.products.append(prod)
            for k in prod["ings"]:
                for w in k:
                    self.by_word[w].add(idx)
            if prod["brand"]:
                self.by_brand[prod["brand"]].add(idx)
                self.by_brand[prod["brand"].split()[0]].add(idx)


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


def candidate_brand(index, text):
    """Longest leading word run (1-4 words) that is an RxNorm brand name."""
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\.\-]*", text)
    for n in range(min(4, len(toks)), 0, -1):
        cand = " ".join(toks[:n]).lower()
        if cand in index.by_brand:
            return cand
    return ""


def canon_words(index, ws):
    out = set()
    for w in ws:
        if w not in index.by_word and w not in SALTS and w.endswith("ate") and (w[:-3] + "ic") in index.by_word:
            out |= {w[:-3] + "ic", "acid"}           # alendronate -> alendronic acid (RxNorm naming)
        else:
            out.add(w)
    return out


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
