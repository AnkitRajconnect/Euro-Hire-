"""Offline tests for conservative France-first direct company discovery."""
from crawler.france_discovery import (
    API_SEARCH,
    _candidate_from_result,
    discover_france,
    import_discoveries,
)
from crawler.company_registry import connect


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payloads[url]


def valid_company(**overrides):
    item = {
        "siren": "123456789",
        "nom_raison_sociale": "EXEMPLE INDUSTRIE",
        "etat_administratif": "A",
        "nature_juridique": "5710",
        "categorie_entreprise": "PME",
        "siege": {
            "caractere_employeur": "O",
            "tranche_effectif_salarie": "11",
            "annee_tranche_effectif_salarie": "2024",
            "activite_principale": "25.11Z",
        },
    }
    item.update(overrides)
    return item


def test_only_active_official_french_legal_person_is_a_candidate():
    candidate = _candidate_from_result(valid_company(), "industrie")
    assert candidate is not None
    assert candidate["registry_id"] == "123456789"
    assert candidate["country"] == "FR"
    assert candidate["registry_status"] == "active"
    assert candidate["raw"]["employer_scope"] == "employer"
    assert candidate["official_domain"] is None
    assert candidate["domain_verified"] is False
    assert "annuaire-entreprises.data.gouv.fr/entreprise/123456789" in candidate["raw"]["official_registry_record"]


def test_rejects_fake_or_unverified_identity_fields():
    assert _candidate_from_result(valid_company(siren="123"), "x") is None
    assert _candidate_from_result(valid_company(nom_raison_sociale=""), "x") is None
    assert _candidate_from_result(valid_company(etat_administratif="C"), "x") is None
    assert _candidate_from_result(valid_company(etat_administratif=""), "x") is None
    assert _candidate_from_result(valid_company(nature_juridique="1000"), "x") is None


def test_unknown_employer_flag_stays_unknown_not_auto_promoted():
    item = valid_company(siege={"caractere_employeur": "", "tranche_effectif_salarie": ""})
    candidate = _candidate_from_result(item, "services")
    assert candidate is not None
    assert candidate["raw"]["employer_scope"] == "unknown"
    assert candidate["domain_verified"] is False


def test_discovery_deduplicates_siren_and_obeys_limit():
    http = FakeHttp({API_SEARCH: {"results": [
        valid_company(),
        valid_company(siren="987654321", nom_raison_sociale="AUTRE SOCIETE"),
        valid_company(),
    ]}})
    result = discover_france(http, limit=2, pages_per_query=1, max_queries=1, queries=("industrie",))
    assert [x["registry_id"] for x in result] == ["123456789", "987654321"]
    assert http.calls[0][1]["params"]["etat_administratif"] == "A"
    assert http.calls[0][1]["params"]["per_page"] == 25


def test_import_preserves_existing_canonical_registry_identity_and_never_verifies_domain(tmp_path):
    db = tmp_path / "companies.sqlite3"
    con = connect(db)
    con.execute(
        """INSERT INTO companies
        (country,registry_id,legal_name,legal_person,registry_status,registry_source,
         registry_checked_at,created_at,updated_at,raw_json)
        VALUES ('FR','123456789','Canonical SIRENE Name',1,'active','INSEE SIRENE',
                '2026-11-23T00:00:00+00:00','2026-11-23','2026-11-23','{}')"""
    )
    con.commit()
    con.close()

    from crawler.france_discovery import import_discoveries
    candidate = _candidate_from_result(valid_company(), "industrie")
    assert import_discoveries(db, [candidate]) == 1

    con = connect(db)
    row = con.execute("SELECT * FROM companies WHERE country='FR' AND registry_id='123456789'").fetchone()
    assert row["legal_name"] == "Canonical SIRENE Name"
    assert row["registry_source"] == "INSEE SIRENE"
    assert row["official_domain"] is None
    assert row["domain_verified"] == 0
    assert row["career_discovery_status"] is None
    con.close()
