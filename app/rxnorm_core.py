"""RxNorm name parsing shared by the 2027 drug-list matcher (formulary_2027/match_rxnorm.py) and the
engine's drug resolver (app/drug_resolver.py). Moved here unchanged on 2026-10-07 so both read drug
names with the same rules: ingredient words, salts, dose forms, release types, strengths."""
import re
from collections import defaultdict

ABBR = {"hcl": "hydrochloride", "hbr": "hydrobromide", "na": "sodium", "k": "potassium", "mag": "magnesium",
        "succ": "succinate", "tart": "tartrate", "ace": "acetate", "phos": "phosphate", "sulf": "sulfate",
        "bitart": "bitartrate", "dipropionate": "dipropionate", "w": "", "hctz": "hydrochlorothiazide", "acetonide": "acetonide",
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
