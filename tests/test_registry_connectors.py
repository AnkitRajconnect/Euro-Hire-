"""Offline contract tests for the added national-company-register connectors."""
import csv
import json
from pathlib import Path

from crawler.company_registry import (
    _brreg_companies,
    _classify_fr_employer_range,
    import_fr_stock_file,
    _companies_house_companies,
    _cro_companies,
    _prh_companies,
    connect,
    import_mapped_registry_file,
    inspect_registry_file,
)


class RegistryHttp:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        key = (url, kwargs.get("params", {}).get("start_index", 0), kwargs.get("params", {}).get("offset", 0),
               kwargs.get("params", {}).get("page", 0))
        return self.payloads[key] if key in self.payloads else self.payloads[url]


def test_companies_house_normalizes_companies_and_uses_api_key(monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    http = RegistryHttp({"https://api.company-information.service.gov.uk/search/companies": {
        "total_results": 2,
        "items": [
            {"company_number": "01234567", "title": "Example Limited", "company_status": "active",
             "company_type": "ltd", "registered_office_address": {"locality": "London"}},
            {"company_number": "OC123456", "title": "Example LLP", "company_status": "active",
             "company_type": "llp"},
            {"company_number": "12345678", "title": "Unincorporated record", "company_status": "active",
             "company_type": "limited-partnership"},
        ]
    }})
    out = _companies_house_companies("Example", 10, http)
    assert [x["registry_id"] for x in out] == ["01234567", "OC123456"]
    assert all(x["country"] == "GB" and x["registry_status"] == "active" for x in out)
    assert http.calls[0][1]["auth"] == ("test-key", "")


def test_cro_uses_cc_by_dataset_and_excludes_non_commercial_types():
    http = RegistryHttp({"https://opendata.cro.ie/api/3/action/datastore_search": {
        "success": True,
        "result": {"total": 3, "records": [
            {"company_num": 12345, "company_name": "Example Limited", "company_status": "Normal",
             "company_type": "Private Company Limited by Shares", "company_type_code": 1},
            {"company_num": 12346, "company_name": "Example CLG", "company_status": "Normal",
             "company_type": "Company Limited by Guarantee", "company_type_code": 2},
            {"company_num": 12347, "company_name": "Closed Limited", "company_status": "Dissolved",
             "company_type": "Designated Activity Company", "company_type_code": 3},
        ]}
    }})
    out = _cro_companies("Example", 10, http)
    assert len(out) == 2
    assert out[0]["registry_id"] == "12345" and out[0]["registry_status"] == "active"
    assert out[1]["registry_id"] == "12347" and out[1]["registry_status"] == "inactive"
    assert http.calls[0][1]["params"]["resource_id"]


def test_prh_only_accepts_known_trade_register_company_forms():
    http = RegistryHttp({"https://avoindata.prh.fi/opendata-ytj-api/v3/companies": {
        "totalResults": 2,
        "companies": [
            {"businessId": {"value": "1234567-8"}, "names": [{"name": "Example Oy"}],
             "companyForms": [{"type": "OY", "version": 1, "descriptions": [{"description": "Osakeyhtiö"}]}],
             "website": {"url": "https://example.fi"}, "tradeRegisterStatus": "Registered",
             "lastModified": "2026-11-20"},
            {"businessId": {"value": "2345678-9"}, "names": [{"name": "Private trader"}],
             "companyForms": [{"type": "TMI", "descriptions": [{"description": "Yksityinen elinkeinonharjoittaja"}]}]},
            {"businessId": {"value": "3456789-0"}, "names": [{"name": "Pending Oy"}],
             "companyForms": [{"type": "OY", "version": 1, "descriptions": [{"description": "Osakeyhtiö"}]}],
             "tradeRegisterStatus": "Pending"},
        ]
    }})
    out = _prh_companies("Example", 10, http)
    assert len(out) == 2
    assert out[0]["registry_id"] == "1234567-8"
    assert out[0]["registry_status"] == "active"
    assert out[1]["registry_status"] == "pending"
    assert out[0]["official_domain"] == "https://example.fi"
    assert out[0]["domain_verified"] is True


def test_brreg_excludes_sole_proprietors_and_marks_dissolved_inactive():
    http = RegistryHttp({"https://data.brreg.no/enhetsregisteret/api/enheter": {
        "_embedded": {"enheter": [
            {"organisasjonsnummer": "923456789", "navn": "Example AS",
             "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
             "hjemmeside": "https://example.no", "konkurs": False},
            {"organisasjonsnummer": "923456780", "navn": "Sole trader",
             "organisasjonsform": {"kode": "ENK", "beskrivelse": "Enkeltpersonforetak"}},
            {"organisasjonsnummer": "923456781", "navn": "Dissolved AS",
             "organisasjonsform": {"kode": "AS"}, "slettedato": "2026-11-01"},
        ]},
        "page": {"totalPages": 1}
    }})
    out = _brreg_companies("Example", 10, http)
    assert [x["registry_id"] for x in out] == ["923456789", "923456781"]
    assert out[1]["registry_status"] == "inactive"
    assert out[0]["official_domain"] == "https://example.no"


def test_estonia_mapped_open_data_import_requires_explicit_commercial_forms(tmp_path):
    csv_file = tmp_path / "ee.csv"
    with csv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["registry_code", "legal_name", "status", "legal_form", "website"])
        writer.writeheader()
        writer.writerows([
            {"registry_code": "10000001", "legal_name": "Example OÜ", "status": "Entered", "legal_form": "osaühing", "website": "example.ee"},
            {"registry_code": "10000002", "legal_name": "Example NGO", "status": "Entered", "legal_form": "non-profit association", "website": "ngo.ee"},
        ])
    assert inspect_registry_file(csv_file) == ["registry_code", "legal_name", "status", "legal_form", "website"]

    mapping = {
        "number_column": "registry_code", "name_column": "legal_name", "status_column": "status",
        "active_values": ["Entered"], "inactive_values": ["In liquidation"],
        "legal_form_column": "legal_form", "allowed_legal_forms": ["osaühing"],
        "website_column": "website", "snapshot_date": "2026-11-23",
    }
    mapping_file = tmp_path / "ee-mapping.json"
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    con = connect(tmp_path / "registry.sqlite3")
    count = import_mapped_registry_file(con, "EE", csv_file, mapping_file,
                                        "Estonia e-Business Register Open Data")
    row = con.execute("SELECT * FROM companies WHERE country='EE' AND registry_id='10000001'").fetchone()
    assert count == 1
    assert row["legal_name"] == "Example OÜ"
    assert row["official_domain"] == "example.ee"
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    con.close()


def test_estonia_import_rejects_empty_legal_form_allowlist(tmp_path):
    csv_file = tmp_path / "ee.csv"
    csv_file.write_text("id,name,status,form\n1,A,Entered,osaühing\n", encoding="utf-8")
    mapping = {"number_column": "id", "name_column": "name", "status_column": "status",
               "active_values": ["Entered"], "legal_form_column": "form", "allowed_legal_forms": [],
               "snapshot_date": "2026-11-23"}
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    con = connect(tmp_path / "registry.sqlite3")
    try:
        import_mapped_registry_file(con, "EE", csv_file, mapping_file, "EE test")
    except ValueError as exc:
        assert "allowed_legal_forms" in str(exc)
    else:
        raise AssertionError("Expected an explicit legal-form allowlist")
    finally:
        con.close()


def test_gleif_search_imports_global_lei_records_without_credentials(tmp_path):
    from crawler.company_registry import _gleif_companies, import_candidates

    payload = {
        "data": [
            {"id": "5493001KJTIIGC8Y1R12", "attributes": {
                "lei": "5493001KJTIIGC8Y1R12",
                "entity": {
                    "legalName": {"name": "Example Europe NV"},
                    "status": "ACTIVE",
                    "legalAddress": {"country": "BE", "city": "Brussels", "addressLines": ["Example Street 1"]},
                    "legalForm": {"id": "6J"},
                    "registeredAt": {"id": "RA000463"},
                    "registeredAs": "BE0123456789",
                },
                "registration": {"status": "ISSUED", "lastUpdateDate": "2026-11-20"},
            }},
            {"id": "213800D1EI4B9WTWWD28", "attributes": {
                "lei": "213800D1EI4B9WTWWD28",
                "entity": {
                    "legalName": {"name": "Example Holdings BV"},
                    "status": "INACTIVE",
                    "legalAddress": {"country": "NL", "city": "Amsterdam"},
                    "legalForm": {"id": "54"},
                },
                "registration": {"status": "LAPSED"},
            }},
            {"id": "5493003KJTIIGC8Y1R99", "attributes": {
                "lei": "5493003KJTIIGC8Y1R99",
                "entity": {
                    "legalName": {"name": "Example Lapsed LEI NV"},
                    "status": "ACTIVE",
                    "legalAddress": {"country": "BE", "city": "Antwerp"},
                    "legalForm": {"id": "6J"},
                },
                "registration": {"status": "LAPSED"},
            }},
        ],
        "meta": {"pagination": {"total": 2}},
    }

    http = RegistryHttp({"https://api.gleif.org/api/v1/lei-records": payload})
    items = _gleif_companies("Example", 10, http)
    assert len(items) == 3
    assert items[0]["country"] == "BE"
    assert items[0]["registry_id"] == "5493001KJTIIGC8Y1R12"
    assert items[0]["registry_status"] == "active"
    assert "official_domain" not in items[0]
    assert items[1]["registry_status"] == "inactive"
    assert items[2]["registry_status"] == "pending"  # active entity, but lapsed LEI
    assert http.calls[0][1]["params"]["filter[entity.legalName]"] == "Example"
    assert "auth" not in http.calls[0][1]

    con = connect(tmp_path / "gleif.sqlite3")
    try:
        import_candidates(con, items)
        row = con.execute("SELECT * FROM companies WHERE country='BE' AND registry_id=?", ("5493001KJTIIGC8Y1R12",)).fetchone()
        assert row["legal_name"] == "Example Europe NV"
        assert row["registry_source"] == "GLEIF Global LEI Index"
        assert row["official_domain"] is None
        assert row["domain_verified"] == 0
        assert json.loads(row["raw_json"])["national_registration_number"] == "BE0123456789"
    finally:
        con.close()


def test_gleif_country_filter_is_applied_and_unknown_status_stays_pending():
    from crawler.company_registry import _gleif_companies

    payload = {"data": [
        {"id": "5493001KJTIIGC8Y1R12", "attributes": {
            "entity": {"legalName": {"name": "Example BE"}, "status": "ACTIVE", "legalAddress": {"country": "BE"}},
            "registration": {"status": "ISSUED"},
        }},
        {"id": "213800D1EI4B9WTWWD28", "attributes": {
            "entity": {"legalName": {"name": "Example NL"}, "status": "UNKNOWN", "legalAddress": {"country": "NL"}},
            "registration": {"status": "LAPSED"},
        }},
    ]}
    http = RegistryHttp({"https://api.gleif.org/api/v1/lei-records": payload})
    out = _gleif_companies("Example", 10, http, country="NL")
    assert len(out) == 1
    assert out[0]["country"] == "NL"
    assert out[0]["registry_status"] == "pending"
    assert "official_domain" not in out[0]



def test_france_employer_scope_uses_sirene_employee_bands():
    assert _classify_fr_employer_range("NN") == "non_employer"
    assert _classify_fr_employer_range("00") == "non_employer"
    assert _classify_fr_employer_range("01") == "employer"
    assert _classify_fr_employer_range("53") == "employer"
    assert _classify_fr_employer_range("") == "unknown"
    assert _classify_fr_employer_range("XX") == "unknown"


def test_france_stock_import_records_employer_scope_and_requires_employee_field(tmp_path):
    csv_file = tmp_path / "fr.csv"
    csv_file.write_text("\n".join([
        "siren;name;status;form;effectif;diffusion",
        "123456789;Employeur SAS;A;5710;11;O",
        "234567890;No employee SAS;A;5710;NN;O",
        "345678901;Unknown count SAS;A;5710;;O",
    ]) + "\n", encoding="utf-8")
    mapping = {
        "number_column": "siren", "name_column": "name", "status_column": "status",
        "active_values": ["A"], "inactive_values": ["C"], "legal_form_column": "form",
        "legal_form_prefixes": ["5"], "employee_range_column": "effectif",
        "diffusion_column": "diffusion", "excluded_diffusion_values": ["P", "N"],
        "snapshot_date": "2026-11-23",
    }
    mapping_file = tmp_path / "fr-mapping.json"
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    con = connect(tmp_path / "fr-registry.sqlite3")
    try:
        assert import_fr_stock_file(con, csv_file, mapping_file) == 3
        rows = {r["registry_id"]: json.loads(r["raw_json"]) for r in con.execute(
            "SELECT registry_id,raw_json FROM companies WHERE country='FR'")}
        assert rows["123456789"]["employer_scope"] == "employer"
        assert rows["123456789"]["employee_range"] == "11"
        assert rows["234567890"]["employer_scope"] == "non_employer"
        assert rows["345678901"]["employer_scope"] == "unknown"
    finally:
        con.close()



def test_france_headcount_evidence_survives_registry_refresh_without_a_band(tmp_path):
    from crawler.company_registry import upsert_candidate

    con = connect(tmp_path / "registry.sqlite3")
    upsert_candidate(con, country="FR", registry_id="123456789", legal_name="Employer SAS",
                     registry_status="active", registry_source="INSEE SIRENE stock", legal_person=True,
                     raw={"employee_range": "11", "employee_range_date": "2023",
                          "employer_scope": "employer"})
    upsert_candidate(con, country="FR", registry_id="123456789", legal_name="Employer SAS",
                     registry_status="active", registry_source="INSEE SIRENE API", legal_person=True,
                     raw={"employee_range": None, "employee_range_date": None,
                          "employer_scope": "unknown", "activity_code": "62.01Z"})
    row = con.execute("SELECT raw_json FROM companies WHERE country='FR'").fetchone()
    payload = json.loads(row["raw_json"])
    assert payload["employer_scope"] == "employer"
    assert payload["employee_range"] == "11"
    assert payload["activity_code"] == "62.01Z"
    con.close()
