"""Claude goes through AWS Bedrock only (2026-10-08, BAA). Never api.anthropic.com; any failure -> names as written.
No network: the Bedrock client is faked."""
import io
import json
import pytest
import app.main as M

ITEM = [{"original": "Zofran", "normalized": "Zofran", "ingredient": "ondansetron", "brand": "Zofran",
         "dosage": "4 mg", "confidence": 0.97, "flag": ""}]
DRUGS = [{"name": "Zofran", "dosage": "4 mg"}]


class FakeClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeBedrock:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke_model(self, modelId, body):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"body": io.BytesIO(json.dumps(r).encode())}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)

    def no_anthropic(*a, **k):
        raise AssertionError("the engine must never call api.anthropic.com or post drug names anywhere else")
    monkeypatch.setattr(M.requests, "post", no_anthropic)

    def install(replies):
        fake = FakeBedrock(replies)
        monkeypatch.setattr(M, "_BEDROCK_CLIENT", fake)
        return fake
    return install


def ok(items=ITEM, wrap=False):
    text = json.dumps(items)
    if wrap:
        text = "```json\n" + text + "\n```"
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def test_normalizes_through_bedrock_with_default_model(aws):
    fake = aws([ok()])
    out = M.normalize_drugs(DRUGS)
    assert out[0]["ingredient"] == "ondansetron"
    call = fake.calls[0]
    assert call["modelId"] == "us.anthropic.claude-sonnet-4-6"
    assert call["body"]["anthropic_version"] == "bedrock-2023-05-31"
    assert call["body"]["temperature"] == 0
    assert "Zofran" in call["body"]["messages"][0]["content"]


def test_model_comes_from_settings(aws, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
    fake = aws([ok()])
    M.normalize_drugs(DRUGS)
    assert fake.calls[0]["modelId"] == "us.anthropic.claude-sonnet-5-5"


def test_fenced_reply_is_read(aws):
    aws([ok(wrap=True)])
    assert M.normalize_drugs(DRUGS)[0]["ingredient"] == "ondansetron"


def test_model_that_rejects_temperature_is_retried_without_it(aws):
    fake = aws([FakeClientError("ValidationException", "temperature is not supported for this model"), ok()])
    assert M.normalize_drugs(DRUGS)[0]["ingredient"] == "ondansetron"
    assert "temperature" in fake.calls[0]["body"] and "temperature" not in fake.calls[1]["body"]


def test_access_denied_falls_back_to_names_as_written(aws, capsys):
    aws([FakeClientError("AccessDeniedException", "Zofran 4 mg secret client text")])
    out = M.normalize_drugs(DRUGS)
    assert out[0]["normalized"] == "Zofran" and out[0]["dosage"] == "4 mg" and out[0]["ingredient"] == ""
    log = capsys.readouterr().out
    assert "AccessDeniedException" in log
    assert "Zofran" not in log and "secret client text" not in log      # no client data in logs


def test_not_configured_makes_no_call_and_falls_back(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setattr(M, "_BEDROCK_CLIENT", FakeBedrock([]))
    monkeypatch.setattr(M.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback to Anthropic")))
    out = M.normalize_drugs(DRUGS)
    assert out[0]["normalized"] == "Zofran" and out[0]["dosage"] == "4 mg"
    assert M._BEDROCK_CLIENT.calls == []


def test_engine_code_has_no_anthropic_direct_call():
    src = open(M.__file__, encoding="utf-8").read()
    assert "api.anthropic.com/v1" not in src
    assert "x-api-key" not in src


def test_low_confidence_guess_is_not_priced_as_a_drug(aws):
    guess = [{"original": "water pill", "normalized": "furosemide", "ingredient": "furosemide", "brand": "",
              "dosage": "", "confidence": 0.4, "flag": "Generic term; could be several diuretics"}]
    aws([ok(guess)])
    out = M.normalize_drugs([{"name": "water pill", "dosage": ""}])[0]
    assert out["normalized"] == "water pill" and out["ingredient"] == "" and out["brand"] == ""
    assert "possibly furosemide" in out["flag"] and "several diuretics" in out["flag"]


def test_confident_answer_is_kept(aws):
    sure = [{"original": "metforman", "normalized": "metformin", "ingredient": "metformin", "brand": "",
             "dosage": "500 mg", "confidence": 0.97, "flag": "Misspelling of metformin"}]
    aws([ok(sure)])
    out = M.normalize_drugs([{"name": "metforman", "dosage": "500"}])[0]
    assert out["normalized"] == "metformin" and out["ingredient"] == "metformin"


def test_health_shows_claude_setup_without_the_key(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-value")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    with M.app.test_client() as c:
        r = c.get("/health")
    if r.status_code != 200:
        pytest.skip("no local database for /health")
    body = r.get_data(as_text=True)
    assert r.get_json()["claude"] == {"via": "aws-bedrock", "configured": True, "model": "us.anthropic.claude-sonnet-4-6"}
    assert "AKIAFAKE" not in body and "fake-secret-value" not in body
