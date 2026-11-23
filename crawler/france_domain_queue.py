"""Export a rotating review queue for French employers without verified websites.

SIRENE identifies legal entities but does not provide a dependable public company
website field. This queue never guesses a domain. Its cursor advances between
runs so later SIRENs are not permanently starved by the first batch.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .company_registry import DEFAULT_DB, connect


def build_queue(db: Path, limit: int = 1000) -> list[dict]:
    batch_size = max(1, min(int(limit), 5000))
    con = connect(db)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS france_domain_queue_state (
            queue_name TEXT PRIMARY KEY,
            last_registry_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )""")
        cursor = con.execute(
            "SELECT last_registry_id FROM france_domain_queue_state WHERE queue_name='FR_EMPLOYERS'"
        ).fetchone()
        last_id = str(cursor["last_registry_id"] or "") if cursor else ""

        where = """country='FR' AND registry_status='active' AND legal_person=1
                   AND COALESCE(json_extract(raw_json, '$.employer_scope'),'unknown')='employer'
                   AND (domain_verified=0 OR official_domain IS NULL OR official_domain='')"""
        rows = con.execute(
            f"""SELECT registry_id, legal_name, registry_status, official_domain,
                       domain_verified, raw_json
                FROM companies WHERE {where} AND registry_id > ?
                ORDER BY registry_id LIMIT ?""",
            (last_id, batch_size),
        ).fetchall()
        selected = list(rows)
        if len(selected) < batch_size and last_id:
            wrapped = con.execute(
                f"""SELECT registry_id, legal_name, registry_status, official_domain,
                           domain_verified, raw_json
                    FROM companies WHERE {where} AND registry_id <= ?
                    ORDER BY registry_id LIMIT ?""",
                (last_id, batch_size - len(selected)),
            ).fetchall()
            selected.extend(wrapped)

        result = []
        for row in selected:
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
                "next_action": "identify the employer's own website from independent evidence; verify the SIREN or legal name on that site before approval",
            })

        if result:
            con.execute(
                """INSERT INTO france_domain_queue_state(queue_name,last_registry_id,updated_at)
                   VALUES('FR_EMPLOYERS',?,datetime('now'))
                   ON CONFLICT(queue_name) DO UPDATE SET
                   last_registry_id=excluded.last_registry_id,updated_at=excluded.updated_at""",
                (result[-1]["siren"],),
            )
            con.commit()
        return result
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=1000, help="Review batch size; the cursor rotates through the full queue")
    parser.add_argument("--output", default="/tmp/france-domain-review-queue.json")
    args = parser.parse_args(argv)
    items = build_queue(Path(args.db), args.limit)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"country": "FR", "count": len(items), "companies": items},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported the next {len(items)} French employer records needing official-domain verification to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
