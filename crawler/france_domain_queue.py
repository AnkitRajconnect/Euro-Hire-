"""Export a bounded review queue for French employers without verified websites.

SIRENE identifies legal entities but does not provide a reliable official-domain
field. This queue links each SIREN to its official public company record; it
never guesses a domain from a company name.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .company_registry import DEFAULT_DB, connect


def build_queue(db: Path, limit: int = 500) -> list[dict]:
    con = connect(db)
    try:
        rows = con.execute(
            """SELECT registry_id, legal_name, registry_status, official_domain,
                      domain_verified, raw_json
               FROM companies
               WHERE country='FR' AND registry_status='active' AND legal_person=1
                 AND COALESCE(json_extract(raw_json, '$.employer_scope'),'unknown')='employer'
                 AND (domain_verified=0 OR official_domain IS NULL OR official_domain='')
               ORDER BY registry_id
               LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        result = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            siren = str(row["registry_id"])
            result.append({
                "siren": siren,
                "legal_name": row["legal_name"],
                "registry_status": row["registry_status"],
                "activity_code": raw.get("activity_code"),
                "employee_range": raw.get("employee_range"),
                "employee_range_date": raw.get("employee_range_date"),
                "domain_candidate": row["official_domain"],
                "domain_verified": bool(row["domain_verified"]),
                "official_registry_record": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}",
                "next_action": "identify and verify the company's own website; do not infer a domain from its name",
            })
        return result
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--output", default="/tmp/france-domain-review-queue.json")
    args = parser.parse_args(argv)
    items = build_queue(Path(args.db), args.limit)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"country": "FR", "count": len(items), "companies": items},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(items)} French employer records needing official-domain verification to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
