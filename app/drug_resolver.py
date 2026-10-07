"""Drug name -> RxNorm product IDs (RxCUIs), offline (2026-10-07).

Why: names came back "couldn't identify" whenever they weren't RxNorm's exact wording - device
names (Trelegy Ellipta, Lantus SoloStar), release forms (Metformin ER), brands that went generic
(Januvia), abbreviations (HCTZ, KCl) and misspellings. Each was patched one at a time. This
resolver reads the name the way a pharmacist would, against a local copy of RxNorm
(app/data/rxnorm_concepts.json.gz, refreshed with formulary_2027/download_rxnorm.py), and is scored
against tests/data/drug_cases.psv on every test run.

    resolve("Januvia", "100 mg") -> Resolution(rxcuis=[Januvia 100 mg products],
                                               generic=[sitagliptin 100 mg products], ...)

Rules
  * A BRAND name gives that brand's products; their generic twins (same ingredients, strength,
    form, release) come back separately so the engine can say "covered as generic".
  * A GENERIC name gives every product (generic and brand) with exactly those ingredients.
  * Strength, dose form and release (ER/DR) narrow the list when the text gives them; if a stated
    strength matches nothing (insulin "20 units" is a dose, not a strength) the list is not narrowed.
  * A misspelling is read as the closest real name ONLY when one name is clearly closest, and is
    always flagged for the agent to confirm. Nothing else is guessed: no match = empty result, and
    the engine falls back to RxNav, then to "verify the name".
"""
import difflib
import gzip
import json
import os
import re
import threading
from dataclasses import dataclass, field

from app.rxnorm_core import (FORM_WORDS, SALTS, RxIndex, candidate_brand, canon_words, close, form_of,
                             numbers, release_of, words)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rxnorm_concepts.json.gz")

ABBREVIATIONS = {             # whole words, as written on intake sheets
    "hctz": "hydrochlorothiazide", "kcl": "potassium chloride", "asa": "aspirin", "ntg": "nitroglycerin",
    "mtx": "methotrexate", "apap": "acetaminophen", "hcq": "hydroxychloroquine", "b12": "vitamin b12",
    "d3": "cholecalciferol", "cl": "chloride", "succ": "succinate", "tart": "tartrate", "pot": "potassium",
    "asprin": "aspirin", "cd": "er", "xt": "er",          # diltiazem CD / XT are extended release
}
PHRASES = {"vitamin d3": "cholecalciferol", "vit d3": "cholecalciferol", "cyanocobalamin": "vitamin b12",
           "vit b12": "vitamin b12"}
# Real RxNorm ingredients that are also everyday words: never the ONLY basis for a match ("water pill").
EVERYDAY_WORDS = {"water", "oxygen", "alcohol", "air", "sugar", "salt", "blood", "honey", "milk", "coffee",
                  "pill", "pills", "heart", "pressure", "medication", "medicine", "vitamins", "same", "last",
                  "year", "unknown", "machine", "drops",
                  # instructions that happen to be words inside some RxNorm ingredient name
                  "daily", "evening", "skin", "stomach", "weekly", "whole", "with", "acid", "thyroid",
                  # plain-English descriptions: never "corrected" into a drug name
                  "sleeping", "sleep", "cholesterol", "diabetes", "nerve", "muscle", "relaxer", "antibiotic",
                  "allergy", "cough", "cold", "stool", "softener", "laxative", "supplement", "medicine", "pain",
                  "anxiety", "depression", "mood", "nausea", "acid", "reflux", "arthritis", "gout", "prostate",
                  "bladder", "memory", "seizure", "migraine", "infection", "cream", "ointment", "lotion"}
NOT_A_DRUG_INGREDIENT = {"sensor"}          # Abilify MyCite's sensor: only when the text says so
# Everyday words that happen to start an RxNorm brand name ("Eye Wash", "Take Action"): never a brand alone.
NOT_A_BRAND = {"acid", "after", "by", "chew", "eye", "eyes", "morning", "take", "water", "daily", "night",
               "pain", "sleep", "heart", "blood", "sugar", "insulin", "vitamin", "vitamins"}
DEVICE_WORDS = {"ellipta", "solostar", "flexpen", "flextouch", "kwikpen", "tempo", "penfill", "inpen", "clickject",
                "sensoready", "respimat", "diskus", "handihaler", "pressair", "aerosphere", "digihaler",
                "redihaler", "twisthaler", "neohaler", "inhub", "max", "junior", "pen", "pens", "inhaler", "hfa",
                "autoinjector", "vial", "cartridge"}
SPELL_CUTOFF = 0.8           # difflib ratio a misspelling must reach
SPELL_MARGIN = 0.04          # ... and beat the runner-up by


@dataclass
class Resolution:
    rxcuis: list = field(default_factory=list)       # what was written (brand -> the brand's products)
    generic: list = field(default_factory=list)      # brand only: its generic twins
    matched: str = ""                                # what it was read as, e.g. "Januvia" / "lisinopril"
    how: str = ""                                    # "brand" | "ingredient" | ""
    flag: str = ""                                   # for the agent, e.g. spelling read as ...
    strength_matched: bool = True
    fix_from: str = ""                               # spelling: the word as written ...
    fix_to: str = ""                                 # ... and the real name it was read as

    def __bool__(self):
        return bool(self.rxcuis)


_lock = threading.Lock()
_state = {}


def _load():
    with _lock:
        if "index" not in _state:
            with gzip.open(DATA, "rt", encoding="utf-8") as f:
                concepts = json.load(f)["concepts"]
            index = RxIndex(concepts)
            proper = {c["name"].lower(): c["name"] for c in concepts if c["tty"] in ("BN", "IN", "PIN", "MIN")}
            vocab = {w for w in index.by_word if len(w) >= 5 and w not in FORM_WORDS and w not in SALTS}
            vocab |= {b for b in index.by_brand if " " not in b and len(b) >= 5}
            vocab = sorted(vocab)
            sounds = {}
            for v in vocab:
                sounds.setdefault(_sound(v), []).append(v)
            _state.update(index=index, vocab=vocab, sounds=sounds, proper=proper)
    return _state["index"], _state["vocab"]


def _sound(word):
    """A rough sound-alike key for spelling: Plavics ~ Plavix, Zarelto ~ Xarelto, Metphormin ~ Metformin."""
    w = word.lower()
    for a, b in (("ph", "f"), ("ck", "k"), ("cs", "ks"), ("x", "ks"), ("qu", "kw"), ("z", "s"), ("y", "i")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    w = re.sub(r"^ks", "s", w)                            # Xarelto ~ Zarelto
    return re.sub(r"(.)\1+", r"\1", w)                   # double letters


def _prepare(name, dosage):
    t = f"{name or ''} {dosage or ''}".lower()
    t = re.sub(r"(?<![\d.])\.(\d)", r"0.\1", t)                        # ".075 mg" -> "0.075 mg"
    t = re.sub(r"(\d)\s*(mg|mcg|meq|units?)\b", r"\1 \2", t)              # "40mg" -> "40 mg"

    def explode(m):                                                       # "5/325 mg" -> "5 mg , 325 mg"
        nums = [x for x in m.group(1, 2, 3) if x]
        unit = m.group(4) or ""
        return " , ".join(f"{n} {unit}".strip() for n in nums) + " "
    t = re.sub(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?\s*(mg|mcg)?(?!\s*/?\s*ml)", explode, t)
    for phrase, real in PHRASES.items():
        t = re.sub(rf"\b{re.escape(phrase)}\b", real, t)
    t = " ".join(ABBREVIATIONS.get(w, w) for w in re.split(r"(\s+)", t) if w) if False else \
        re.sub(r"[a-z0-9]+", lambda m: ABBREVIATIONS.get(m.group(0), m.group(0)), t)
    return t


def _name_part(text):
    """The words before the first number: where the drug's name is."""
    return re.split(r"\d", text, maxsplit=1)[0]


def _narrow(products, text):
    """Soft filters: strength, dose form, release - each only when the text gives it. A filter that would
    leave nothing is skipped. Nothing is assumed: unwritten form or release keeps every version, and
    the engine then uses the plan's best tier among them (as RxNav's lookup always did)."""
    out = [p for p in products if not p["pack"]] or products
    if re.search(r"\b(pack|kit|starter|titration)\b", text):
        out = [p for p in products if p["pack"]] or out
    if not re.search(r"\b(mycite|sensor)\b", text):
        out = [p for p in out if not any(w in p["name"].lower() for w in NOT_A_DRUG_INGREDIENT)] or out
    nums = numbers(text)
    strength_ok = True
    if nums:
        hit = [p for p in out if all(close(s, nums) for s in p["strengths"])]
        if hit:
            # Every number written must be one of the product's strengths too, when that's possible:
            # Vytorin 10/40 is not the 10/10 tablet. (Soft: "1 tab" is a number but not a strength.)
            groups = [numbers(g) for g in re.findall(r"\d+(?:\.\d+)?\s*(?:mg|mcg|g|meq|units?|%)?", text)]
            both = [p for p in hit if all(any(close(v, p["strengths"]) for v in g) for g in groups if g)]
            out = both or hit
            pct = {float(x) * 10 for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)}   # 1% = 10 mg/mL
            if pct:
                out = [p for p in out if any(close(s, pct) for s in p["strengths"])] or out
        else:
            strength_ok = False
    rel = release_of(text)
    if rel != "ir":                 # ONLY when ER / XL / DR is written. "Omeprazole 20 mg" must keep the
        out = [p for p in out if p["rel"] == rel] or out     # delayed-release capsule everyone takes
                                                             # (2026-10-07: assuming plain release picked a rare tablet)
    form, sub = form_of(text)
    if form:
        same = [p for p in out if p["form"][0] == form]
        if sub:
            same = [p for p in same if p["form"][1] == sub] or same
        out = same or out
    return out, strength_ok


def _generic_twins(index, products):
    twins = set()
    for p in products:
        first = next(iter(p["ings"][0])) if p["ings"] and p["ings"][0] else None
        if first is None:
            continue
        key = sorted(map(sorted, p["ings"]))
        for i in index.by_word.get(first, ()):
            g = index.products[i]
            if g["brand"] or g["pack"] != p["pack"] or g["rel"] != p["rel"] or g["form"] != p["form"]:
                continue
            if any(w in g["name"].lower() for w in NOT_A_DRUG_INGREDIENT):
                continue
            if sorted(map(sorted, g["ings"])) != key:
                continue
            if len(g["strengths"]) == len(p["strengths"]) and all(close(a, [b]) for a, b in
                                                                 zip(sorted(g["strengths"]), sorted(p["strengths"]))):
                twins.add(g["rxcui"])
    return sorted(twins)


def _core_brand(brand):
    """'Lantus SoloStar' -> 'lantus': the brand without device words."""
    return " ".join(w for w in brand.lower().split() if w not in DEVICE_WORDS)


def _by_brand(index, text):
    brand = candidate_brand(index, _name_part(text))
    if not brand or brand in NOT_A_BRAND or brand in FORM_WORDS:
        return None
    pool = [index.products[i] for i in index.by_brand.get(brand, ())]
    if not pool:
        return None
    exact = [p for p in pool if _core_brand(p["brand"]) == _core_brand(brand)]   # Humalog, not Humalog Mix
    pool = exact or pool
    chosen, ok = _narrow(pool, text)
    if not ok and not any(p["form"][0] == "inj" for p in pool):
        # Written at a strength this brand isn't made in (Prilosec 20 mg): read it as the GENERIC at
        # that strength, if one exists, and say so. Never for injectables, whose written number is a
        # dose (Ozempic 0.5 mg), and never onto another brand.
        ings = {" ".join(sorted(k)) for p in pool for k in p["ings"]}
        rest = re.sub(rf"\b{re.escape(brand)}\b", " ", text)
        alt = _by_ingredient(index, " ".join(sorted(ings)) + " " + rest, generic_only=True)
        if alt and alt.strength_matched:
            alt.flag = "not made in that strength - priced as the generic"
            return alt
    return Resolution(rxcuis=sorted({p["rxcui"] for p in chosen}), generic=_generic_twins(index, chosen),
                      matched=brand, how="brand", strength_matched=ok)


PINNED = {"vitamin b12": "vitamin b12"}   # names whose digits matter (words() drops digits)


def _by_ingredient(index, text, generic_only=False):
    tw = canon_words(index, set(words(text)))
    for phrase, in_name in PINNED.items():
        if phrase in text:
            hits = [p for p in index.products if in_name in p["name"].lower() and len(p["ings"]) == 1]
            chosen, ok = _narrow(hits, text)
            return Resolution(rxcuis=sorted({p["rxcui"] for p in chosen}), matched=phrase, how="ingredient",
                              strength_matched=ok) if chosen else None
    drug_words = [w for w in tw if w not in FORM_WORDS and w not in SALTS and len(w) > 2 and w in index.by_word]
    if not drug_words:
        drug_words = [w for w in tw if w in SALTS and w in index.by_word]          # potassium chloride
    if not drug_words or all(w in EVERYDAY_WORDS for w in drug_words):
        return None
    row_salts = {w for w in tw if w in SALTS}
    pool = set()
    for w in drug_words:
        pool |= index.by_word.get(w, set())
    hits = []
    for i in pool:
        p = index.products[i]
        if not all(k <= tw for k in p["ings"]):                      # every ingredient of the product named
            continue
        if {w for w in drug_words if w not in EVERYDAY_WORDS} - p["ing_words"] - SALTS:   # text names more
            continue
        if row_salts and p["salts"] and not (p["salts"] <= row_salts):                  # mononitrate vs dinitrate
            continue
        if generic_only and p["brand"]:
            continue
        hits.append(p)
    if not hits:
        return None
    if row_salts:                                                   # prednisolone ACETATE, not the plain one
        hits = [p for p in hits if p["salts"] and p["salts"] <= row_salts] or hits
    chosen, ok = _narrow(hits, text)
    names = sorted({" + ".join(sorted(" ".join(sorted(k)) for k in p["ings"])) for p in chosen})
    return Resolution(rxcuis=sorted({p["rxcui"] for p in chosen}), matched=names[0] if names else "",
                      how="ingredient", strength_matched=ok)


def _exact(index, text):
    return _by_brand(index, text) or _by_ingredient(index, text)


SPELL_SURE = 0.88             # without a written strength, a correction must be at least this close


def _spelling_candidates(index, text):
    """(misspelled word, [(score, real name), ...] best first) for the first word that isn't a real name."""
    vocab, sounds = _state["vocab"], _state["sounds"]
    for tok in re.findall(r"[a-z]{4,}", _name_part(text)):
        if tok in FORM_WORDS or tok in SALTS or tok in EVERYDAY_WORDS or tok in DEVICE_WORDS:
            continue
        if tok in index.by_word or tok in index.by_brand:
            continue
        cands = set(difflib.get_close_matches(tok, vocab, n=5, cutoff=SPELL_CUTOFF))
        for key in difflib.get_close_matches(_sound(tok), list(sounds), n=5, cutoff=SPELL_CUTOFF):
            cands.update(sounds[key])
        score = lambda v: max(difflib.SequenceMatcher(None, tok, v).ratio(),
                              difflib.SequenceMatcher(None, _sound(tok), _sound(v)).ratio())
        scored = sorted(((score(v), v) for v in cands), reverse=True)
        return tok, [x for x in scored if x[0] >= SPELL_CUTOFF][:5]
    return None, []


def _by_spelling(index, text):
    """A misspelling is read as a real drug only when that reading is backed up:
      * if a strength is written, the corrected drug must come in that strength (Zofran 8 mg is never
        read as a skin gel; Amaryl 4 mg never as a vaccine);
      * if no strength is written, the spelling must be very close (SPELL_SURE);
      * and no different drug may be about as close - then a person decides.
    Always flagged for the agent to confirm."""
    wrong, scored = _spelling_candidates(index, text)
    if not scored:
        return None
    has_strength = bool(numbers(text))
    passing = []
    for sc, right in scored:
        found = _exact(index, re.sub(rf"\b{wrong}\b", right, text))
        if not found:
            continue
        if has_strength and not found.strength_matched:
            continue
        if not has_strength and sc < SPELL_SURE:
            continue
        passing.append((sc, right, found))
    if not passing:
        return None
    best = passing[0]
    for sc, right, found in passing[1:]:
        if best[0] - sc < SPELL_MARGIN and set(found.rxcuis) != set(best[2].rxcuis):
            return None                                   # two different drugs about equally close
    found = best[2]
    found.flag = "check spelling"
    found.fix_from, found.fix_to = wrong, best[1]
    return found


def resolve(name, dosage="", alternates=()):
    """Best reading of one drug as written. Empty Resolution = not identified.

    alternates: other names for the same drug from the AI clean-up step - its ingredient name above
    all, which is how retired brands (Zofran, Amaryl, Imdur) are read. Tried after the name as
    written and before any spelling correction."""
    if not (name or "").strip():
        return Resolution()
    index, _ = _load()
    text = _prepare(name, dosage)
    found = _exact(index, text)
    if found:
        return found
    for alt in alternates or ():
        if alt and alt.strip() and alt.strip().lower() != (name or "").strip().lower():
            found = _exact(index, _prepare(alt, dosage))
            if found:
                return found
    return _by_spelling(index, text) or Resolution()


def display_name(word):
    """RxNorm's own capitalisation for a brand ("Plavix", "NovoLog"); ingredients get a capital first letter."""
    _load()
    proper = _state["proper"].get(word.lower())
    if proper and any(ch.isupper() for ch in proper):
        return proper
    return word[:1].upper() + word[1:]


def corrected(name, res):
    """The name as written, with the misspelled word replaced: "Atorvastin 40" -> "Atorvastatin 40"."""
    if not res.fix_from:
        return name
    return re.sub(rf"(?i)\b{re.escape(res.fix_from)}\b", display_name(res.fix_to), name, count=1)


def suggestions(name, dosage="", limit=3):
    """Closest real drug names for a name that couldn't be identified, best first ("Did you mean ...?").
    Unlike automatic correction, these are for a PERSON to choose from, so the strength rule doesn't
    apply - but each must be a name the resolver can read."""
    if not (name or "").strip():
        return []
    index, _ = _load()
    text = _prepare(name, dosage)
    wrong, scored = _spelling_candidates(index, text)
    out = []
    for _, right in scored:
        if _exact(index, re.sub(rf"\b{wrong}\b", right, text)):
            label = re.sub(rf"(?i)\b{re.escape(wrong)}\b", display_name(right), name, count=1)
            if label not in out:
                out.append(label)
        if len(out) >= limit:
            break
    return out


def check(name, dosage=""):
    """What the agent should see for one drug on the Details tab (engine endpoint /check-drugs):
    status  ok | spelling | strength | unknown | empty
    suggestion  the corrected name (spelling) ;  candidates  closest names (unknown)"""
    name = (name or "").strip()
    if not name:
        return {"status": "empty", "suggestion": "", "candidates": [], "read_as": "", "note": ""}
    res = resolve(name, dosage)
    if res and res.flag == "check spelling":
        return {"status": "spelling", "suggestion": corrected(name, res), "candidates": [],
                "read_as": display_name(res.matched), "note": f'Looks like "{corrected(name, res)}"'}
    if res and res.flag:
        return {"status": "strength", "suggestion": "", "candidates": [], "read_as": display_name(res.matched),
                "note": res.flag}
    if res:
        note = "" if res.strength_matched or not numbers(_prepare("", dosage)) else \
            "This strength wasn't found for this drug - check the dosage"
        return {"status": "ok", "suggestion": "", "candidates": [], "read_as": display_name(res.matched),
                "note": note}
    cands = suggestions(name, dosage)
    return {"status": "unknown", "suggestion": "", "candidates": cands, "read_as": "",
            "note": "Not recognized" + (" - did you mean one of these?" if cands else " - check the name")}


def warm():
    """Build the index in the background at app start (about a second) so the first request is fast."""
    threading.Thread(target=_load, daemon=True).start()
