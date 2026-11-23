import json

from crawler.company_registry import connect, upsert_candidate
from crawler.france_domain_queue import build_queue


def test_france_domain_queue_only_lists_active_unverified_employers(tmp_path):
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    try:
        upsert_candidate(
            con, country="FR", registry_id="123456789", legal_name="French Employer SAS",
            registry_status="active", registry_source="INSEE SIRENE stock", legal_person=True,
            raw={"employer_scope": "employer", "employee_range": "11", "activity_code": "47.11A"},
            registry_checked_at="2026-11-23",
        )
        upsert_candidate(
            con, country="FR", registry_id="223456789", legal_name="French Non Employer SAS",
            registry_status="active", registry_source="INSEE SIRENE stock", legal_person=True,
            raw={"employer_scope": "non_employer"},
            registry_checked_at="2026-11-23",
        )
        upsert_candidate(
            con, country="FR", registry_id="323456789", legal_name="Verified Employer SAS",
            registry_status="active", registry_source="INSEE SIRENE stock", legal_person=True,
            official_domain="https://verified.example", domain_verified=True,
            domain_method="manual_documented_review",
            raw={"employer_scope": "employer"},
            registry_checked_at="2026-11-23",
        )
        con.commit()
    finally:
        con.close()

    rows = build_queue(db, 100)
    assert [row["siren"] for row in rows] == ["123456789"]
    assert rows[0]["official_registry_record"].endswith("/123456789")
    assert rows[0]["domain_verified"] is False
    assert "do not infer" in rows[0]["next_action"]
