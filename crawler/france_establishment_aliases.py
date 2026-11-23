"""Build French employer trading-name aliases from the official SIRENE establishment stock.

Only active establishments of already-identified, diffusible French employer
legal entities are considered. Establishment trading names are candidate-name
aliases, never website verification evidence.
"""
from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from typing import Callable

from .company_registry import now_iso
from .http import Http

DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret/"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS france_company_name_aliases (
  siren TEXT NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source_siret TEXT,
  postcode TEXT,
  locality TEXT,
  source_field TEXT NOT NULL,
  source_release TEXT NOT NULL,
  PRIMARY KEY (siren, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_france_alias_siren
  ON france_company_name_aliases(siren);
CREATE INDEX IF NOT EXISTS idx_france_alias_normalized
  ON france_company_name_aliases(normalized_alias);
CREATE TABLE IF NOT EXISTS france_establishment_alias_sync (
  source TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL,
  modified TEXT,
  synced_at TEXT NOT NULL,
  establishments_scanned INTEGER NOT NULL,
  alias_values_seen INTEGER NOT NULL,
  alias_rows_stored INTEGER NOT NULL
);
"""


def latest_establishment_parquet(http: Http) -> dict:
    dataset = http.get_json(DATASET_API, headers={"Accept": "application/json"})
    resources = dataset.get("resources") or []
    candidates = [
        item for item in resources
        if "stocketablissement" in str(item.get("title") or "").casefold()
        and "historique" not in str(item.get("title") or "").casefold()
        and str(item.get("format") or "").casefold() == "parquet"
        and item.get("url")
    ]
    if not candidates:
        raise RuntimeError("The official SIRENE dataset has no current StockEtablissement Parquet resource.")
    return max(
        candidates,
        key=lambda item: str(item.get("last_modified") or item.get("created_at") or ""),
    )


def _write_employer_sirens(con, path: Path) -> int:
    cursor = con.execute(
        """SELECT registry_id FROM companies
           WHERE country='FR' AND registry_status='active' AND legal_person=1
             AND json_extract(raw_json, '$.employer_scope')='employer'
             AND length(registry_id)=9"""
    )
    count = 0
    with path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["siren"])
        while True:
            batch = cursor.fetchmany(50000)
            if not batch:
                break
            writer.writerows((str(row[0]),) for row in batch)
            count += len(batch)
    return count


def sync_establishment_aliases(con, normalize_name: Callable[[str], set[str]],
                               *, force: bool = False) -> dict:
    """Sync alias names from current active SIRENE establishments.

    The 2.1GB-class official Parquet file is queried remotely with DuckDB
    httpfs. Only employer SIRENs and the small subset of name/address columns
    are read into the local SQLite database.
    """
    import duckdb

    con.executescript(SCHEMA)
    http = Http(min_delay=0.5, timeout=60, retries=3)
    resource = latest_establishment_parquet(http)
    resource_id = str(resource.get("id") or resource["url"])
    modified = str(resource.get("last_modified") or resource.get("created_at") or "")
    previous = con.execute(
        "SELECT resource_id,modified FROM france_establishment_alias_sync WHERE source='FR_STOCK_ETABLISSEMENT'"
    ).fetchone()
    if (not force and previous and previous["resource_id"] == resource_id
            and (previous["modified"] or "") == modified):
        stored = con.execute("SELECT COUNT(*) FROM france_company_name_aliases").fetchone()[0]
        return {
            "ok": True, "updated": False, "skipped": True,
            "resource_id": resource_id, "modified": modified,
            "alias_rows_stored": stored,
            "reason": "Current SIRENE establishment release is already indexed",
        }

    ddb = duckdb.connect()
    scanned = aliases_seen = 0
    inserted_total = 0
    try:
        ddb.execute("INSTALL httpfs")
        ddb.execute("LOAD httpfs")
        ddb.execute("SET threads=4")
        with tempfile.TemporaryDirectory(prefix="eurohire-sirene-establishments-") as temp:
            siren_csv = Path(temp) / "employer-sirens.csv"
            employer_count = _write_employer_sirens(con, siren_csv)
            if employer_count == 0:
                raise RuntimeError("No active French employer SIRENs are available for establishment alias matching.")

            parquet_url = str(resource["url"]).replace("'", "''")
            csv_path = str(siren_csv).replace("'", "''")
            query = f"""
                SELECT e.siren, e.siret,
                       e.enseigne1Etablissement, e.enseigne2Etablissement,
                       e.enseigne3Etablissement, e.denominationUsuelleEtablissement,
                       e.codePostalEtablissement, e.libelleCommuneEtablissement
                FROM read_parquet('{parquet_url}') AS e
                INNER JOIN read_csv_auto('{csv_path}', header=true, all_varchar=true) AS employers
                  ON e.siren = employers.siren
                WHERE e.etatAdministratifEtablissement = 'A'
                  AND COALESCE(e.statutDiffusionEtablissement, '') NOT IN ('P','N')
                  AND (
                    NULLIF(TRIM(e.enseigne1Etablissement), '') IS NOT NULL
                    OR NULLIF(TRIM(e.enseigne2Etablissement), '') IS NOT NULL
                    OR NULLIF(TRIM(e.enseigne3Etablissement), '') IS NOT NULL
                    OR NULLIF(TRIM(e.denominationUsuelleEtablissement), '') IS NOT NULL
                  )
            """
            cursor = ddb.execute(query)
            pending: list[tuple] = []
            while True:
                batch = cursor.fetchmany(25000)
                if not batch:
                    break
                for (siren, siret, enseigne1, enseigne2, enseigne3,
                     usual_name, postcode, locality) in batch:
                    scanned += 1
                    pieces = [
                        ("enseigne1Etablissement", str(enseigne1 or "").strip()),
                        ("enseigne2Etablissement", str(enseigne2 or "").strip()),
                        ("enseigne3Etablissement", str(enseigne3 or "").strip()),
                        ("denominationUsuelleEtablissement", str(usual_name or "").strip()),
                    ]
                    candidates = [(field, value) for field, value in pieces
                                  if value and value.upper() not in {"[ND]", "ND", "N/A"}]
                    combined = " ".join(value for _, value in pieces[:3] if value and value.upper() not in {"[ND]", "ND", "N/A"}).strip()
                    if combined and len([value for _, value in candidates if value in combined]) > 1:
                        candidates.append(("enseigne_combined", combined))

                    seen_norms = set()
                    for field, alias in candidates:
                        keys = normalize_name(alias)
                        if not keys:
                            continue
                        normalized = min(keys, key=lambda key: (len(key), key))
                        if normalized in seen_norms:
                            continue
                        seen_norms.add(normalized)
                        aliases_seen += 1
                        pending.append((
                            str(siren), alias, normalized, str(siret or ""),
                            str(postcode or ""), str(locality or ""), field, modified or resource_id,
                        ))
                if len(pending) >= 100000:
                    before = con.total_changes
                    con.executemany(
                        """INSERT INTO france_company_name_aliases
                           (siren,alias,normalized_alias,source_siret,postcode,locality,source_field,source_release)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(siren,normalized_alias) DO UPDATE SET
                             alias=excluded.alias, source_siret=excluded.source_siret,
                             postcode=excluded.postcode, locality=excluded.locality,
                             source_field=excluded.source_field,source_release=excluded.source_release""",
                        pending,
                    )
                    con.commit()
                    inserted_total += con.total_changes - before
                    pending.clear()

            if pending:
                before = con.total_changes
                con.executemany(
                    """INSERT INTO france_company_name_aliases
                       (siren,alias,normalized_alias,source_siret,postcode,locality,source_field,source_release)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(siren,normalized_alias) DO UPDATE SET
                         alias=excluded.alias, source_siret=excluded.source_siret,
                         postcode=excluded.postcode, locality=excluded.locality,
                         source_field=excluded.source_field,source_release=excluded.source_release""",
                    pending,
                )
                con.commit()
                inserted_total += con.total_changes - before

        # Retain only aliases present in the successfully scanned current stock.
        con.execute("DELETE FROM france_company_name_aliases WHERE source_release<>?",
                    (modified or resource_id,))
        con.execute(
            """INSERT INTO france_establishment_alias_sync
               (source,resource_id,modified,synced_at,establishments_scanned,alias_values_seen,alias_rows_stored)
               VALUES('FR_STOCK_ETABLISSEMENT',?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 resource_id=excluded.resource_id,modified=excluded.modified,synced_at=excluded.synced_at,
                 establishments_scanned=excluded.establishments_scanned,
                 alias_values_seen=excluded.alias_values_seen,alias_rows_stored=excluded.alias_rows_stored""",
            (resource_id, modified, now_iso(), scanned, aliases_seen,
             con.execute("SELECT COUNT(*) FROM france_company_name_aliases").fetchone()[0]),
        )
        con.commit()
        return {
            "ok": True, "updated": True, "skipped": False,
            "resource_id": resource_id, "modified": modified,
            "active_employer_sirens": employer_count,
            "active_employer_establishments_with_names_scanned": scanned,
            "alias_values_seen": aliases_seen,
            "alias_rows_stored": con.execute("SELECT COUNT(*) FROM france_company_name_aliases").fetchone()[0],
            "sqlite_changes": inserted_total,
        }
    finally:
        ddb.close()
