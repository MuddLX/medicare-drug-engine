"""normalize_drugs must read the reply's TEXT block wherever it is (2026-10-07: newer models can send a
non-text block first; content[0]["text"] then failed and names went through uncleaned)."""
import json
import app.main as M


class _Resp:
    def __init__(self, body):
        self._b = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._b


def _reply(blocks, stop="end_turn"):
    return lambda *a, **k: _Resp({"content": blocks, "stop_reason": stop})


ITEM = [{"original": "Zofran", "normalized": "Zofran", "ingredient": "ondansetron", "brand": "Zofran",
         "dosage": "4 mg", "confidence": 0.97, "flag": ""}]


def test_text_block_after_a_thinking_block(monkeypatch):
    monkeypatch.setattr(M.requests, "post", _reply([{"type": "thinking", "thinking": "..."},
                                                    {"type": "text", "text": json.dumps(ITEM)}]))
    out = M.normalize_drugs([{"name": "Zofran", "dosage": "4 mg"}])
    assert out[0]["ingredient"] == "ondansetron"


def test_fenced_json_is_read(monkeypatch):
    monkeypatch.setattr(M.requests, "post", _reply([{"type": "text", "text": "```json\n" + json.dumps(ITEM) + "\n```"}]))
    assert M.normalize_drugs([{"name": "Zofran", "dosage": "4 mg"}])[0]["ingredient"] == "ondansetron"


def test_cut_off_reply_falls_back_to_names_as_written(monkeypatch):
    monkeypatch.setattr(M.requests, "post", _reply([{"type": "text", "text": "[{\"original\": \"Zof"}], stop="max_tokens"))
    out = M.normalize_drugs([{"name": "Zofran", "dosage": "4 mg"}])
    assert out[0]["normalized"] == "Zofran" and out[0]["dosage"] == "4 mg"
