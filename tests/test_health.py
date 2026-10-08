"""/health says which plan year and data are live - the first check after a switch (2026-10-07)."""
import app.main as M


def test_health_reports_data_year():
    body = M.app.test_client().get("/health").get_json()
    assert body["status"] == "ok"
    assert isinstance(body["data_year"], int) and body["data_year"] >= 2026
    assert body["data_vintage"] and isinstance(body["prices_estimated"], bool)
