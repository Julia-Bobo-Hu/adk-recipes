"""Safe GCP teardown utility defaulting to --dry-run and protecting compliance audit records."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

PROTECTED_BQ_TABLES: tuple[str, ...] = ("decision_ledger", "signoff_refusals", "eval_runs")
EPHEMERAL_BQ_TABLES: tuple[str, ...] = ("sp500_companies", "supply_chain_edges")


def build_cleanup_plan(
    *,
    project_id: str,
    dataset_id: str = "fsi_capital_markets",
    staging_bucket: str = "",
    audit_bucket: str = "",
    dry_run: bool = True,
    include_records: bool = False,
    confirm_project: str = "",
) -> dict[str, Any]:
    """Compute a safe teardown plan enforcing retention guards on audit ledgers."""
    if include_records and confirm_project.strip() != project_id.strip():
        return {
            "status": "refused",
            "dry_run": dry_run,
            "reason": (
                "RETENTION_GUARD_TRIGGERED: Deleting protected audit tables (decision_ledger, "
                "signoff_refusals, eval_runs) or retention-protected audit buckets requires "
                f"both --include-records AND --confirm-project={project_id!r}."
            ),
            "protected_preserved": list(PROTECTED_BQ_TABLES) + ([audit_bucket] if audit_bucket else []),
            "actions": [],
        }

    actions: list[dict[str, str]] = [
        {
            "resource_type": "bigquery_property_graph",
            "target": f"{project_id}.{dataset_id}.disclosed_supply_chain_graph",
            "operation": "would_drop" if dry_run else "drop",
        }
    ]
    for tbl in EPHEMERAL_BQ_TABLES:
        actions.append(
            {
                "resource_type": "bigquery_table",
                "target": f"{project_id}.{dataset_id}.{tbl}",
                "operation": "would_delete" if dry_run else "delete",
            }
        )
    if staging_bucket:
        actions.append(
            {
                "resource_type": "gcs_staging_bucket",
                "target": f"gs://{staging_bucket}",
                "operation": "would_delete" if dry_run else "delete",
            }
        )

    preserved: list[str] = []
    if include_records:
        for tbl in PROTECTED_BQ_TABLES:
            actions.append(
                {
                    "resource_type": "bigquery_audit_table",
                    "target": f"{project_id}.{dataset_id}.{tbl}",
                    "operation": "would_delete_with_override" if dry_run else "delete_with_override",
                }
            )
        if audit_bucket:
            actions.append(
                {
                    "resource_type": "gcs_audit_bucket",
                    "target": f"gs://{audit_bucket}",
                    "operation": "would_delete_with_override" if dry_run else "delete_with_override",
                }
            )
    else:
        preserved = [f"{project_id}.{dataset_id}.{t}" for t in PROTECTED_BQ_TABLES]
        if audit_bucket:
            preserved.append(f"gs://{audit_bucket}")

    return {
        "status": "planned" if dry_run else "ready_to_execute",
        "dry_run": dry_run,
        "include_records": include_records,
        "protected_preserved": preserved,
        "actions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe teardown for Capital Markets Analyst GCP resources")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-gcp-project"))
    parser.add_argument("--dataset", default=os.environ.get("FSI_BQ_DATASET", "fsi_capital_markets"))
    parser.add_argument("--staging-bucket", default=os.environ.get("FSI_GCS_STAGING_BUCKET", ""))
    parser.add_argument("--audit-bucket", default=os.environ.get("FSI_GCS_AUDIT_BUCKET", ""))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--execute", dest="dry_run", action="store_false")
    parser.add_argument("--include-records", action="store_true", default=False)
    parser.add_argument("--confirm-project", default="")
    args = parser.parse_args()

    plan = build_cleanup_plan(
        project_id=args.project,
        dataset_id=args.dataset,
        staging_bucket=args.staging_bucket,
        audit_bucket=args.audit_bucket,
        dry_run=args.dry_run,
        include_records=args.include_records,
        confirm_project=args.confirm_project,
    )
    print(json.dumps(plan, indent=2))
    if plan["status"] == "refused":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
