"""What to price when the sheet gives no dose, and when the written amount isn't the amount priced (2026-10-31).

Jill's notes: clients often leave the dose off, and sometimes write an unusual amount ("2 tabs twice daily").
Drug prices come from Medicare.gov Plan Finder searches, captured at ONE standard amount per drug.

* No dose written -> price Medicare.gov's usual strength: the strength Medicare.gov picks when you search the drug
  by name alone. Those are the labels from the original top-drug batches (no "added" mark in
  app/data/mpf_drug_map.json). Before this the engine priced whichever strength came first, silently (blank
  gabapentin was priced as 100 mg: $215 a year on Aetna instead of $371 for the usual 300 mg).
  - A drug captured only by a later fill (no usual strength known) is priced at the one strength on file, if there
    is exactly one, and says so.
  - Two strengths/forms equally likely (ketoconazole 2% cream or shampoo) -> nothing is guessed: the cost is left out
    and the agent is asked for the dose.
  Always flagged so the agent confirms.
* An amount written on the sheet that differs from the amount the price is for -> flagged, never re-priced (scaling
  a price would be an estimate; only official numbers are shown as fact).
"""
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mpf_drug_map.json")


@lru_cache(maxsize=1)
def _labels():
    """[(label, rxcuis, is_medicare_gov_default)]"""
    with open(MAP, encoding="utf-8") as f:
        drugs = json.load(f)["drugs"]
    return [(label, frozenset(v.get("rxcuis") or ()), "added" not in v) for label, v in drugs.items()]


def reload():
    _labels.cache_clear()


_AMOUNT_WORDS = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:tabs?|tablets?|caps?|capsules?|pills?|puffs?|drops?|times?|x)\b"
    r"|\bevery\s+\d+\s*(?:hours?|hrs?|days?)\b|\bq\d+h\b", re.I)


def strip_amounts(dosage):
    """The strength part of a written dose: "1 tab daily" -> "daily", "40 mg 2 times a day" -> "40 mg a day".
    (2026-10-31: the "1" in "1 tab daily" was read as a 1 mg strength, so the drug came out "strength not found".)"""
    return re.sub(r"\s+", " ", _AMOUNT_WORDS.sub(" ", dosage or "")).strip()


def has_strength(*texts):
    """True when a text has a strength number: "300 mg", "Lisinopril 10" - not "Vitamin D3" (part of a word) and
    not "1 tab daily" (an amount)."""
    return any(re.search(r"(?<![A-Za-z])\d", strip_amounts(t)) for t in texts)


def strength_of(label):
    """'Gabapentin 300mg capsule' -> '300mg capsule' (what the report shows as the dose)."""
    m = re.search(r"(?<![A-Za-z])\d.*$", label or "")
    return m.group(0).strip() if m else (label or "")


@dataclass
class DoseChoice:
    kind: str                       # "default" | "only" | "ambiguous"
    label: str = ""
    rxcuis: list = field(default_factory=list)
    options: list = field(default_factory=list)

    @property
    def strength(self):
        return strength_of(self.label)

    def flag(self):
        if self.kind == "default":
            return f"no dose on the sheet - priced at Medicare.gov's usual strength ({self.strength}), confirm"
        if self.kind == "only":
            return f"no dose on the sheet - priced at {self.strength}, the only strength on file, confirm"
        return ("no dose on the sheet - more than one strength or form on file ("
                + " / ".join(strength_of(o) for o in self.options) + "), add the dose")


def choose(name, rxcuis, generic=None):
    """The strength to price for a drug written without a dose, or None when we have no prices for it at all."""
    want = set(rxcuis or ()) | set(generic or ())
    hits = [(lab, rx, d) for lab, rx, d in _labels() if rx & want]
    if not hits:
        return None
    defaults = [h for h in hits if h[2]]
    pool, kind = (defaults, "default") if defaults else (hits, "only")
    if len(pool) > 1:
        # brand vs generic written: "Synthroid" -> the Synthroid label, "levothyroxine" -> the generic's
        first = (name or "").strip().lower().split()[0] if (name or "").strip() else ""
        same = [h for h in pool if first and h[0].lower().startswith(first)]
        if len(same) == 1:
            pool = same
    if len(pool) == 1:
        return DoseChoice(kind, pool[0][0], sorted(pool[0][1]))
    return DoseChoice("ambiguous", options=sorted(h[0] for h in pool))


def label_for(rxcuis):
    """The captured label whose products these are (one label per strength), or ''."""
    want = set(rxcuis or ())
    for lab, rx, _ in _labels():
        if rx & want:
            return lab
    return ""


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "half": 0.5, "1/2": 0.5}
_TIMES = [  # (pattern, fills per day) - most specific first
    (r"\b(qid|four times|4 times|4x|every 6 hours|q6h)\b", 4),
    (r"\b(tid|three times|3 times|3x|every 8 hours|q8h)\b", 3),
    (r"\b(bid|twice|two times|2 times|2x|every 12 hours|q12h)\b", 2),
    (r"\b(once|daily|qd|every day|each day|a day|per day|nightly|at bedtime|qhs|every morning|in the morning|"
     r"every evening|in the evening)\b", 1),
]


def monthly_amount(dosage):
    """Tablets/capsules a month the sheet's wording implies ("40 mg twice daily" -> 60), or None when it says no
    amount, or says "as needed" (unknowable)."""
    t = (dosage or "").lower()
    if not t or re.search(r"\b(as needed|prn|as directed)\b", t):
        return None
    m = re.search(r"\b(\d+(?:\.\d+)?|one|two|three|four|half|1/2)\s*(tabs?|tablets?|caps?|capsules?|pills?)\b", t)
    units = (_WORDS.get(m.group(1)) or float(m.group(1))) if m else 1
    if re.search(r"\b(weekly|once a week|every week|qw)\b", t):
        return round(units * 4)
    if re.search(r"\bevery other day\b", t):
        return round(units * 15)
    for pat, per_day in _TIMES:
        if re.search(pat, t):
            return round(units * per_day * 30)
    if m:                                   # "2 tabs" with no frequency: can't tell a month
        return None
    return None


def amount_note(conn, rxcuis, dosage):
    """Flag text when the sheet's amount differs from the amount this drug's price is for, else ''."""
    return amount_check(conn, rxcuis, dosage)[0]


def amount_check(conn, rxcuis, dosage):
    """(flag text, amount priced a month) when the sheet's amount differs from the priced amount, else ('', None)."""
    implied = monthly_amount(dosage)
    if implied is None:
        return "", None
    label = label_for(rxcuis)
    if not label or not re.search(r"\b(tablet|capsule)", label.lower()):
        return "", None                    # liquids, inhalers, creams: amounts aren't counted in pills
    want = sorted(set(rxcuis or ()))
    if not want:
        return "", None
    row = conn.execute(f"SELECT quantity FROM mpf_price WHERE rxcui IN ({','.join('?' * len(want))}) "
                       "AND quantity IS NOT NULL LIMIT 1", want).fetchone()
    try:
        priced = round(float(row[0])) if row else None
    except (TypeError, ValueError):
        priced = None
    if not priced or priced == implied:
        return "", None
    return (f"sheet says \"{dosage.strip()}\" (about {implied} a month) - priced at Medicare.gov's standard "
            f"{priced} a month, verify"), priced


# What the report and the app show in the dose column (engine-side wording, so no new app version is needed).
ASSUMED_SUFFIX = " (usual dose, none on sheet)"
ONLY_SUFFIX = " (only strength on file, none on sheet)"     # not Medicare.gov's pick: the one strength we have prices for
NEEDED_TEXT = "no dose on sheet - add it"


def priced_for_suffix(priced):
    return f" (priced for {priced} a month)"
