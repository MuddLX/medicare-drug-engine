"""Drugs written with a release-form abbreviation (ER, XL, XR, SR, CR, DR, LA, 24HR, 12HR).
Found 2026-10-02 on Gerald Okafor: "Metformin ER 1000mg" came back "Not identified" because
RxNav's exact search doesn't understand "ER". RxNav is mocked here (no network in tests)."""
from urllib.parse import urlparse, parse_qs
import app.main as M


class R:
    def __init__(self, data): self._d = data
    def json(self): return self._d


APPROX = {
    "metformin er 1000mg": [("1807894", "METFORMIN HYDROCHLORIDE 1000 mg ORAL TABLET, EXTENDED RELEASE"),
                            ("1807888", "METFORMIN HYDROCHLORIDE 1000 mg ORAL TABLET, EXTENDED RELEASE")],
    "bupropion xl 150mg": [("993545", "BUPROPION (WELLBUTRIN XL) 150MG SA TAB"),
                           ("555", "SOMETHING ELSE 150 MG")],
}
RELATED = {"993545": ["993541"], "1807894": [], "1807888": []}


def fake_get(url, timeout=None):
    u = urlparse(url); q = parse_qs(u.query)
    fake_get.calls.append(u.path)
    if u.path.endswith("/drugs.json"):
        return R({"drugGroup": {"name": None}})
    if u.path.endswith("/rxcui.json"):
        return R({"idGroup": {}})
    if u.path.endswith("/approximateTerm.json"):
        term = q["term"][0].lower()
        cands = [{"rxcui": r, "rank": "1", "name": n} for r, n in APPROX.get(term, [])]
        return R({"approximateGroup": {"candidate": cands}})
    if "/related.json" in u.path:
        rx = u.path.split("/rxcui/")[1].split("/")[0]
        return R({"relatedGroup": {"conceptGroup": [{"tty": "SCD", "conceptProperties":
                  [{"rxcui": x} for x in RELATED.get(rx, [])]}]}})
    raise AssertionError("unexpected url " + url)


def setup(monkeypatch):
    fake_get.calls = []
    monkeypatch.setattr(M.requests, "get", fake_get)


def test_metformin_er_is_identified(monkeypatch):
    setup(monkeypatch)
    rx = M.lookup_rxcuis("Metformin ER", "1000mg")
    assert {"1807894", "1807888"} <= set(rx)


def test_brand_match_also_brings_its_generic(monkeypatch):
    setup(monkeypatch)
    rx = M.lookup_rxcuis("Bupropion XL", "150mg")
    assert "993541" in rx and "993545" in rx
    assert "555" not in rx                      # a candidate that isn't bupropion is ignored


def test_misspelling_without_release_form_is_not_guessed(monkeypatch):
    setup(monkeypatch)
    assert M.lookup_rxcuis("Lisinpril", "10mg") == []
    assert not any("approximateTerm" in p for p in fake_get.calls)


def test_metformin_er_1000_maps_to_the_local_formulary():
    conn = M.get_db()
    n = conn.execute("SELECT COUNT(*) FROM formulary WHERE rxcui IN ('1807894','1807888')").fetchone()[0]
    conn.close()
    assert n > 0
