"""Sync the official French SIRENE legal-unit stock into the local registry.

The full stock is used for company coverage, not as permission to crawl.
Only employers with a verified official domain and approved career source
can enter the vacancy crawler.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .company_registry import connect, import_fr_stock_file, print_france_coverage

DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret/"
)


def latest_stock_resource() -> dict:
    request = urllib.request.Request(
        DATASET_API,
        headers={"User-Agent": "Euro-Hire-France-Registry/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        dataset = json.load(response)
    resources = dataset.get("resources") or []
    candidates = [
        item for item in resources
        if "stockunitelegale" in str(item.get("title") or "").casefold()
        and "historique" not in str(item.get("title") or "").casefold()
        and str(item.get("format") or "").casefold() == "zip"
        and item.get("url")
    ]
    if not candidates:
        raise RuntimeError("The official data.gouv.fr dataset has no current StockUniteLegale ZIP resource.")
    return max(
        candidates,
        key=lambda item: str(item.get("last_modified") or item.get("created_at") or ""),
    )


def download_resource(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Euro-Hire-France-Registry/1.0"}
    )
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as out:
        total = response.headers.get("Content-Length")
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if downloaded % (100 * 1024 * 1024) < len(chunk):
                print(f"Downloaded {downloaded // (1024 * 1024)} MiB" +
                      (f" / {int(total) // (1024 * 1024)} MiB" if total else ""))


def sync(db_path: Path, force: bool = False) -> int:
    resource = latest_stock_resource()
    resource_id = str(resource.get("id") or resource.get("url"))
    modified = str(resource.get("last_modified") or resource.get("created_at") or "")
    con = connect(db_path)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS registry_sync_state (
            source TEXT PRIMARY KEY, resource_id TEXT NOT NULL, modified TEXT,
            synced_at TEXT NOT NULL, imported_rows INTEGER NOT NULL
        )""")
        previous = con.execute(
            "SELECT resource_id, modified FROM registry_sync_state WHERE source='FR_SIRENE_STOCK'"
        ).fetchone()
        if not force and previous and previous["resource_id"] == resource_id and previous["modified"] == modified:
            print("SIRENE stock is unchanged; reusing the existing France registry database.")
            print_france_coverage(con)
            return 0

        with tempfile.TemporaryDirectory(prefix="eurohire-sirene-") as temp:
            archive = Path(temp) / "stock-unite-legale.zip"
            print(f"Downloading official SIRENE stock: {resource.get('title')}")
            download_resource(str(resource["url"]), archive)
            if not zipfile.is_zipfile(archive):
                raise RuntimeError("The downloaded SIRENE resource is not a valid ZIP archive.")
            snapshot = datetime.now(timezone.utc).date().isoformat()
            mapping = {
                "number_column": "siren",
                "name_column": "denominationUniteLegale",
                "status_column": "etatAdministratifUniteLegale",
                "active_values": ["A"],
                "inactive_values": ["C"],
                "legal_form_column": "categorieJuridiqueUniteLegale",
                "legal_form_prefixes": ["3", "4", "5", "6", "7", "8", "9"],
                "diffusion_column": "statutDiffusionUniteLegale",
                "excluded_diffusion_values": ["P", "N"],
                "activity_column": "activitePrincipaleUniteLegale",
                "employee_range_column": "trancheEffectifsUniteLegale",
                "employee_range_date_column": "anneeEffectifsUniteLegale",
                "snapshot_date": snapshot,
                "full_snapshot": True,
            }
            mapping_path = Path(temp) / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            count = import_fr_stock_file(con, archive, mapping_path, confirm_full_snapshot=True)
            if count < 1:
                raise RuntimeError("SIRENE import returned zero records; previous registry snapshot was not replaced.")
            con.execute(
                """INSERT INTO registry_sync_state(source,resource_id,modified,synced_at,imported_rows)
                   VALUES('FR_SIRENE_STOCK',?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET resource_id=excluded.resource_id,
                   modified=excluded.modified,synced_at=excluded.synced_at,
                   imported_rows=excluded.imported_rows""",
                (resource_id, modified, datetime.now(timezone.utc).isoformat(), count),
            )
            con.commit()
            print(f"Imported {count:,} French legal-entity records from the official SIRENE stock.")
            print_france_coverage(con)
            return count
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/company_registry.sqlite3")
    parser.add_argument("--force", action="store_true", help="Re-import even if the official stock resource is unchanged")
    args = parser.parse_args(argv)
    sync(Path(args.db), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
