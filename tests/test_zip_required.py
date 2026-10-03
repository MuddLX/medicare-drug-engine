"""A missing or bad ZIP must be refused, never silently replaced with 55441 (2026-10-03: pasted
emails/screenshots make a missing ZIP likely)."""
import pytest
import app.main as M


@pytest.fixture
def client():
    return M.app.test_client()


@pytest.mark.parametrize("route", ["/process-soa", "/client-comparison"])
@pytest.mark.parametrize("zip_code", [None, "", "5544", "55441-1234x", "abcde"])
def test_bad_or_missing_zip_is_refused(client, route, zip_code):
    body = {"client_name": "Test", "drug_names": "Lisinopril", "drug_dosages": "10mg"}
    if zip_code is not None:
        body["zip_code"] = zip_code
    r = client.post(route, json=body)
    assert r.status_code == 400
    assert "ZIP" in r.get_json()["error"]
