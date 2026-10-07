"""2027 doctor/clinic network check (2026-10-06): only 2027 sources; confirmation > directory > system."""
import sqlite3
import pytest
from app import providers_2027 as P

PLANS = [("Medica Essential ($20)", "H8889", "022", "MA"), ("UHC AARP MN-0001", "H2001", "116", "MA"),
         ("UHC AARP FG-0002", "H2001", "119", "MA"), ("Aetna Signature", "H3219", "001", "MA"),
         ("AARP Rx Saver", "S5921", "370", "PD")]


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "p.db")
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE directory (source TEXT, carrier TEXT, plan_scope TEXT, section TEXT, state TEXT, county TEXT,
        city TEXT, clinic_name TEXT, address TEXT, zip TEXT, phone TEXT, health_system TEXT, plan_codes TEXT,
        specialty TEXT, last_name TEXT, first_name TEXT, credentials TEXT, accepting TEXT, page INTEGER)""")
    c.executemany("INSERT INTO directory (carrier, city, clinic_name, health_system, last_name, first_name, credentials, accepting) "
                  "VALUES (?,?,?,?,?,?,?,?)", [
        ("Medica", "Coon Rapids", "COURAGE KENNY REHABILITATION ASSOCIATES", "Allina Health - Medicare Advantage", "Danielson", "Matthew A", "MD", "Y"),
        ("Medica", "Duluth", "ESSENTIA HEALTH DULUTH CLINIC", "Essentia Health - Medicare Advantage", "Danielson", "Mary", "MD", "N"),
    ])
    c.execute("CREATE TABLE systems (system TEXT, patterns TEXT)")
    c.executemany("INSERT INTO systems VALUES (?,?)", [("Allina", "allina|courage kenny"), ("Park Nicollet Methodist", "park nicollet"),
                                                       ("Altru Health", "altru")])
    c.execute("CREATE TABLE system_network (system TEXT, carrier TEXT, status TEXT, detail TEXT, plan_scope TEXT, source TEXT, as_of TEXT)")
    c.executemany("INSERT INTO system_network VALUES (?,?,?,?,'','','')", [
        ("Allina", "UHC", "in", "listed"), ("Allina", "Medica", "in", "240 locations"),
        ("Park Nicollet Methodist", "UHC", "in", "listed"), ("Altru Health", "UHC", "verify", "not found - verify")])
    c.execute("""CREATE TABLE confirmations (provider TEXT, clinic TEXT, system TEXT, carrier TEXT, plans TEXT, status TEXT,
        confirmed_by TEXT, confirmed_on TEXT, note TEXT)""")
    c.execute("INSERT INTO confirmations VALUES ('Dr. Sarah Olson','','','Aetna','all','in','Lacey','10/12/2026','')")
    c.commit(); c.close()
    return p


def st(res, label):
    return res["plans"][label]["status"]


def test_doctor_in_the_medica_directory_and_system_level_for_uhc(db):
    r = P.check([{"first_name": "Matthew", "last_name": "Danielson", "city": "Coon Rapids"}], PLANS, db)[0]
    assert st(r, "Medica Essential ($20)") == "in" and "Courage Kenny" in r["plans"]["Medica Essential ($20)"]["detail"]
    assert r["system"] == "Allina"                         # learned from the Medica listing
    assert st(r, "UHC AARP MN-0001") == "system"            # Allina is in UHC's 2027 network
    assert st(r, "UHC AARP FG-0002") == "not_checked"       # FG-0002 not covered by the UHC source
    assert st(r, "Aetna Signature") == "not_checked"
    assert "AARP Rx Saver" not in r["plans"]                # drug-only plan: no doctors


def test_first_initial_picks_the_right_namesake_and_not_accepting_shows(db):
    r = P.check([{"first_name": "Mary", "last_name": "Danielson"}], PLANS, db)[0]
    assert "Essentia" in r["plans"]["Medica Essential ($20)"]["detail"]
    assert r["plans"]["Medica Essential ($20)"]["accepting"] == "N"


def test_doctor_missing_from_a_full_directory_is_not_found_never_out(db):
    r = P.check([{"first_name": "John", "last_name": "Nobody"}], PLANS, db)[0]
    assert st(r, "Medica Essential ($20)") == "not_found"


def test_clinic_only_uses_system_level(db):
    r = P.check([{"raw_text": "Park Nicollet Clinic", "clinic_name": "Park Nicollet"}], PLANS, db)[0]
    assert st(r, "UHC AARP MN-0001") == "system"
    assert st(r, "Medica Essential ($20)") == "verify"     # no Medica row for that system in this tiny DB -> verify, not "out"


def test_system_marked_verify_stays_verify(db):
    r = P.check([{"clinic_name": "Altru Clinic"}], PLANS, db)[0]
    assert st(r, "UHC AARP MN-0001") == "verify"


def test_agency_confirmation_wins(db):
    r = P.check([{"first_name": "Sarah", "last_name": "Olson"}], PLANS, db)[0]
    assert st(r, "Aetna Signature") == "in" and "Lacey" in r["plans"]["Aetna Signature"]["detail"]


def test_no_database_means_not_checked(tmp_path):
    r = P.check([{"last_name": "Danielson"}], PLANS, str(tmp_path / "missing.db"))[0]
    assert all(v["status"] == "not_checked" for v in r["plans"].values())


def test_unreadable_name_is_not_matched(db):
    r = P.check([{"first_name": "M", "last_name": "Dan...son"}], PLANS, db)[0]
    assert st(r, "Medica Essential ($20)") == "not_found"
