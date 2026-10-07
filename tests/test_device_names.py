"""Device names aren't part of RxNorm brand names (2026-10-07, Dorothy Halvorsen test):
'Trelegy Ellipta' and 'Lantus SoloStar' came back "couldn't identify"."""
import app.main as M


def test_strip_device_words():
    assert M.strip_device_words("Trelegy Ellipta") == "Trelegy"
    assert M.strip_device_words("Lantus SoloStar") == "Lantus"
    assert M.strip_device_words("Toujeo Max SoloStar") == "Toujeo"
    assert M.strip_device_words("Humalog KwikPen") == "Humalog"
    assert M.strip_device_words("Ventolin HFA") == "Ventolin"
    assert M.strip_device_words("Spiriva Respimat") == "Spiriva"
    assert M.strip_device_words("Ozempic pen") == "Ozempic"


def test_strip_device_words_leaves_real_names_alone():
    assert M.strip_device_words("Penicillin VK") == "Penicillin VK"
    assert M.strip_device_words("Eliquis") == "Eliquis"
    assert M.strip_device_words("Inhaler") == ""


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _fake_rxnav(known):
    """RxNav stand-in: drugs.json answers only for exact RxNorm brand names in `known`."""
    def get(url, timeout=None):
        if "drugs.json?name=" in url:
            name = M.requests.utils.unquote(url.split("name=", 1)[1]).strip().lower()
            if name in known:
                return _Resp({"drugGroup": {"conceptGroup": [
                    {"tty": "SBD", "conceptProperties": [{"rxcui": r} for r in known[name]]}]}})
            return _Resp({"drugGroup": {}})
        return _Resp({})
    return get


def test_lookup_retries_without_device_name(monkeypatch):
    monkeypatch.setattr(M.requests, "get", _fake_rxnav({"trelegy": ["1945048"], "lantus": ["847232", "285018"]}))
    assert M.lookup_rxcuis("Trelegy Ellipta", "100/62.5/25 mcg") == ["1945048"]
    assert M.lookup_rxcuis("Lantus SoloStar", "20 units") == ["847232", "285018"]


def test_unknown_drug_still_unidentified(monkeypatch):
    monkeypatch.setattr(M.requests, "get", _fake_rxnav({"trelegy": ["1945048"]}))
    assert M.lookup_rxcuis("Notarealdrug Ellipta", "") == []
