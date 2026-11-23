"""Discover real French legal entities through the official DINUM company-search API.

This is candidate discovery only. A DINUM/RNE/SIRENE identity match is required
before a record is stored; no website is guessed, and no candidate is approved
for crawling or publication by this module.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from .company_registry import (
    DEFAULT_DB,
    _classify_fr_employer_range,
    connect,
    now_iso,
    upsert_candidate,
)
from .http import Http

API_SEARCH = "https://recherche-entreprises.api.gouv.fr/search"
API_SOURCE = "API Recherche d'entreprises (DINUM; RNE/SIRENE)"
SIREN_RE = re.compile(r"^\d{9}$")

# These are search terms, not a whitelist of companies. They provide broad,
# repeatable coverage across French sectors. Each result is still checked
# against the official API response before it is stored.
SEARCH_TERMS = (
    "entreprise", "société", "groupe", "industrie", "commerce",
    "restauration", "hôtellerie", "transport", "logistique", "construction",
    "informatique", "conseil", "distribution", "santé", "services",
    "nettoyage", "sécurité", "agroalimentaire", "automobile", "énergie",
    "communication", "immobilier", "tourisme", "formation",
)


def _candidate_from_result(item: dict, query: str) -> dict | None:
    """Return a strictly identified active French legal-person candidate."""
    if not isinstance(item, dict):
        return None
    siren = str(item.get("siren") or "").strip()
    name = str(item.get("nom_raison_sociale") or item.get("nom_complet") or "").strip()
    legal_form = str(item.get("nature_juridique") or "").strip()
    status = str(item.get("etat_administratif") or "").strip().upper()
    # The official search endpoint is authoritative for legal identity. Still
    # fail closed if the required identifier, name, legal form or status is absent.
    if not SIREN_RE.fullmatch(siren) or not name or status != "A":
        return None
    # Keep companies/legal persons only. Individual entrepreneurs and unknown
    # legal forms are not silently promoted into the employer database.
    if not legal_form.startswith(("3", "4", "5", "6")):
        return None

    head_office = item.get("siege") or {}
    if not isinstance(head_office, dict):
        head_office = {}
    employer_flag = str(head_office.get("caractere_employeur") or "").strip().upper()
    employee_range = str(head_office.get("tranche_effectif_salarie") or "").strip().upper()
    if employer_flag in {"O", "OUI", "YES"}:
        employer_scope = "employer"
    elif employer_flag in {"N", "NON", "NO"}:
        employer_scope = "non_employer"
    else:
        employer_scope = _classify_fr_employer_range(employee_range)

    official_record = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"
    api_record = {
        "discovery_source": API_SOURCE,
        "discovery_query": query,
        "discovered_at": now_iso(),
        "official_registry_record": official_record,
        "api_record_status": status,
        "legal_form_code": legal_form,
        "employer_flag_at_head_office": employer_flag or None,
        "employee_range": employee_range or None,
        "employee_range_date": head_office.get("annee_tranche_effectif_salarie"),
        "activity_code": head_office.get("activite_principale"),
        "company_category": item.get("categorie_entreprise"),
        "creation_date": item.get("date_creation"),
        "employer_scope": employer_scope,
        "website_status": "not_discovered",
        "verification_note": (
            "Legal identity matched to the official French company-search API. "
            "No website or careers page has been inferred or verified."
        ),
    }
    return {
        "country": "FR",
        "registry_id": siren,
        "legal_name": name,
        "legal_form": legal_form,
        "legal_person": True,
        "registry_status": "active",
        "registry_source": API_SOURCE,
        "official_domain": None,
        "domain_verified": False,
        "raw": api_record,
    }


def discover_france(http: Http, *, limit: int = 500, pages_per_query: int = 1,
                    max_queries: int = 24, queries: tuple[str, ...] = SEARCH_TERMS) -> list[dict]:
    """Discover active French companies through bounded official API searches."""
    if limit < 1 or pages_per_query < 1 or max_queries < 1:
        raise ValueError("limit, pages_per_query and max_queries must all be positive")
    found: dict[str, dict] = {}
    for query in queries[:max_queries]:
        for page in range(1, pages_per_query + 1):
            params = {
                "q": query,
                "etat_administratif": "A",
                "minimal": "true",
                "include": "siege",
                "page": page,
                "per_page": 25,
            }
            payload = http.get_json(API_SEARCH, params=params,
                                    headers={"Accept": "application/json"})
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(results, list) or not results:
                break
            for item in results:
                candidate = _candidate_from_result(item, query)
                if candidate is None:
                    continue
                siren = candidate["registry_id"]
                if siren not in found:
                    found[siren] = candidate
                else:
                    # Retain all discovery queries as provenance without
                    # duplicating a company or replacing its legal identity.
                    evidence = found[siren]["raw"].setdefault("also_found_by", [])
                    if query not in evidence:
                        evidence.append(query)
                if len(found) >= limit:
                    return list(found.values())
            if len(results) < 25:
                break
    return list(found.values())


def import_discoveries(db: Path, candidates: list[dict]) -> int:
    """Store official legal-entity candidates, preserving canonical SIRENE data."""
    con = connect(db)
    try:
        for candidate in candidates:
            existing = con.execute(
                "SELECT legal_name,legal_form,registry_source,raw_json FROM companies "
                "WHERE country='FR' AND registry_id=?",
                (candidate["registry_id"],),
            ).fetchone()
            raw = dict(candidate["raw"])
            if existing:
                try:
                    previous = json.loads(existing["raw_json"] or "{}")
                except (TypeError, ValueError):
                    previous = {}
                sources = list(previous.get("additional_registry_sources") or [])
                if API_SOURCE not in sources and existing["registry_source"] != API_SOURCE:
                    sources.append(API_SOURCE)
                raw["additional_registry_sources"] = sources
                # Preserve the SIRENE legal name and registry source as canonical
                # when the same SIREN is already present in the full stock.
                candidate = {
                    **candidate,
                    "legal_name": existing["legal_name"],
                    "legal_form": existing["legal_form"] or candidate["legal_form"],
                    "registry_source": existing["registry_source"] or API_SOURCE,
                    "raw": raw,
                }
            upsert_candidate(con, **candidate, registry_checked_at=now_iso(), commit=False)
        con.commit()
        return len(candidates)
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--max-queries", type=int, default=24)
    parser.add_argument("--output", default="/tmp/france-discovery-report.json")
    args = parser.parse_args(argv)

    candidates = discover_france(
        Http(min_delay=0.5, timeout=25, retries=3),
        limit=args.limit,
        pages_per_query=args.pages_per_query,
        max_queries=args.max_queries,
    )
    imported = import_discoveries(Path(args.db), candidates)
    report = {
        "country": "FR",
        "source": API_SOURCE,
        "discovered": len(candidates),
        "imported": imported,
        "published": 0,
        "website_verified": 0,
        "note": "All records are official-register candidates only. No domains, career pages or jobs were invented or auto-approved.",
        "companies": [
            {
                "siren": item["registry_id"],
                "legal_name": item["legal_name"],
                "legal_form": item["legal_form"],
                "official_registry_record": item["raw"]["official_registry_record"],
                "employer_scope": item["raw"]["employer_scope"],
                "website_status": "not_discovered",
            }
            for item in candidates
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    employer_count = sum(x["raw"]["employer_scope"] == "employer" for x in candidates)
    print(f"Official French companies discovered: {len(candidates)}")
    print(f"Employer evidence at head office: {employer_count}")
    print(f"Imported as unapproved candidates: {imported}")
    print("Websites verified: 0 | Career sources approved: 0 | Published by this step: 0")
    print(f"Review report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
