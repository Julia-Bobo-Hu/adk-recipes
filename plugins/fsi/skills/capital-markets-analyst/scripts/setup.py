"""Keyless ADC GCP Full Setup provisioner for BigQuery Property Graph, XBRL tables, and audit ledgers.

``--execute`` creates the tables and the property graph. ``--load-sample`` (with
``--execute``) also replaces the rows of the two reference tables with the bundled sample
book, so the graph query has data. The audit tables are never written or cleared here.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine  # noqa: E402


def generate_provisioning_ddl(project_id: str, dataset_id: str, graph_name: str) -> str:
    """Build complete BigQuery schema + ISO GQL Property Graph DDL statements."""
    fq = f"`{project_id}.{dataset_id}`"
    base_tables = f"""CREATE SCHEMA IF NOT EXISTS {fq} OPTIONS(location="us-central1");

CREATE TABLE IF NOT EXISTS {fq}.sp500_companies (
  ticker STRING NOT NULL,
  name STRING NOT NULL,
  cik STRING NOT NULL,
  sector STRING NOT NULL,
  filing_doc_id STRING NOT NULL,
  accession_number STRING,
  excerpt_sha256 STRING NOT NULL,
  revenue_m NUMERIC,
  effective_tax_rate NUMERIC,
  diluted_shares_m NUMERIC,
  diluted_eps NUMERIC,
  long_term_debt_m NUMERIC,
  PRIMARY KEY (ticker) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS {fq}.supply_chain_edges (
  edge_id STRING NOT NULL,
  src_ticker STRING NOT NULL,
  dst_ticker STRING NOT NULL,
  relationship STRING NOT NULL,
  revenue_share_pct NUMERIC,
  share_note STRING,
  doc_id STRING NOT NULL,
  section STRING NOT NULL,
  verbatim_quote STRING NOT NULL,
  PRIMARY KEY (edge_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS {fq}.eval_runs (
  run_id STRING NOT NULL,
  started_at_utc TIMESTAMP NOT NULL,
  mode STRING NOT NULL,
  verdict STRING NOT NULL,
  agent_model STRING,
  judge_model STRING,
  dataset_posture STRING NOT NULL,
  gates_json JSON NOT NULL,
  report_json JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS {fq}.decision_ledger (
  timestamp_utc TIMESTAMP NOT NULL,
  packet_sha256 STRING NOT NULL,
  confirm_code_6 STRING NOT NULL,
  maker_email STRING NOT NULL,
  signer_email STRING NOT NULL,
  dual_control BOOL NOT NULL,
  reason_code STRING NOT NULL,
  prev_record_hash STRING NOT NULL,
  record_hash STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS {fq}.signoff_refusals (
  timestamp_utc TIMESTAMP NOT NULL,
  packet_sha256 STRING NOT NULL,
  maker_email STRING NOT NULL,
  signer_email STRING NOT NULL,
  refusal_reason STRING NOT NULL
);
"""
    return base_tables + "\n" + fsi_engine.generate_bigquery_graph_sql(project_id, dataset_id, graph_name)


def split_provisioning_statements(ddl: str) -> list[str]:
    """Split generated DDL into executable statements.

    SQL comment lines are stripped before deciding whether a chunk is empty, so a
    statement that follows a comment header (the property graph) is kept. The
    read-only sample ``GRAPH_TABLE`` query is documentation and is not executed.
    """
    statements: list[str] = []
    for chunk in ddl.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body and not body.upper().startswith("SELECT"):
            statements.append(body)
    return statements


def _sql_string(value: Any) -> str:
    """A BigQuery string literal, or NULL."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"


def _sql_numeric(value: Any) -> str:
    """An exact BigQuery NUMERIC literal in fixed-point form, or NULL.

    Raises:
        decimal.InvalidOperation: ``value`` is not a number.
        ValueError: ``value`` is NaN or infinite.
    """
    if value is None:
        return "NULL"
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"Not a finite number: {value!r}")
    return f"NUMERIC '{number:f}'"


def generate_sample_load_statements(
    project_id: str,
    dataset_id: str,
    dataset: dict[str, Any] | None = None,
) -> list[str]:
    """SQL that replaces the rows of the two reference tables with the bundled sample book.

    Only ``sp500_companies`` and ``supply_chain_edges`` are touched. A link without a
    disclosed share is loaded with a NULL share and its note.
    """
    data = dataset or fsi_engine.load_sample_book()
    fq = f"`{project_id}.{dataset_id}`"
    company_rows = [
        "("
        + ", ".join(
            [
                _sql_string(ticker),
                _sql_string(company["name"]),
                _sql_string(company["cik"]),
                _sql_string(company["sector"]),
                _sql_string(company["filing_doc_id"]),
                _sql_string((company.get("filing") or {}).get("accession_number")),
                _sql_string(company["excerpt_sha256"]),
                _sql_numeric(company["xbrl"]["revenue_m"]),
                _sql_numeric(company["xbrl"]["effective_tax_rate"]),
                _sql_numeric(company["xbrl"]["diluted_shares_m"]),
                _sql_numeric(company["xbrl"]["diluted_eps"]),
                _sql_numeric(company["xbrl"].get("long_term_debt_m")),
            ]
        )
        + ")"
        for ticker, company in data["companies"].items()
    ]
    edge_rows = [
        "("
        + ", ".join(
            [
                _sql_string(edge["edge_id"]),
                _sql_string(edge["src"]),
                _sql_string(edge["dst"]),
                _sql_string(edge["relationship"]),
                _sql_numeric(edge.get("revenue_share_pct")),
                _sql_string(edge.get("share_note") or None),
                _sql_string(edge["doc_id"]),
                _sql_string(edge["section"]),
                _sql_string(edge["verbatim_quote"]),
            ]
        )
        + ")"
        for edge in data["supply_chain_edges"]
    ]
    return [
        f"DELETE FROM {fq}.supply_chain_edges WHERE TRUE",
        f"DELETE FROM {fq}.sp500_companies WHERE TRUE",
        f"INSERT INTO {fq}.sp500_companies (ticker, name, cik, sector, filing_doc_id, accession_number, "
        "excerpt_sha256, revenue_m, effective_tax_rate, diluted_shares_m, diluted_eps, long_term_debt_m) VALUES\n"
        + ",\n".join(company_rows),
        f"INSERT INTO {fq}.supply_chain_edges (edge_id, src_ticker, dst_ticker, relationship, revenue_share_pct, "
        "share_note, doc_id, section, verbatim_quote) VALUES\n" + ",\n".join(edge_rows),
    ]


def run_setup(
    *,
    project_id: str,
    dataset_id: str = "fsi_capital_markets",
    graph_name: str = "disclosed_supply_chain_graph",
    dry_run: bool = True,
    load_sample: bool = False,
) -> dict[str, Any]:
    """Generate or execute Full Setup provisioning via Application Default Credentials.

    With ``load_sample`` the bundled sample book replaces the rows of the two reference
    tables after the tables and the graph exist.
    """
    ddl = generate_provisioning_ddl(project_id, dataset_id, graph_name)
    load_statements = generate_sample_load_statements(project_id, dataset_id) if load_sample else []
    if dry_run:
        return {
            "status": "dry_run_ready",
            "auth_mode": "Application Default Credentials (keyless)",
            "project_id": project_id,
            "dataset_id": dataset_id,
            "graph_name": graph_name,
            "ddl_preview": ddl,
            "sample_load_preview": load_statements,
        }

    import google.auth  # type: ignore[import-untyped]
    from google.cloud import bigquery  # type: ignore[import-untyped]

    credentials, detected_project = google.auth.default()
    target_project = project_id or detected_project
    client = bigquery.Client(project=target_project, credentials=credentials)
    for stmt in split_provisioning_statements(ddl):
        client.query(stmt).result()
    for stmt in load_statements:
        client.query(stmt).result()

    return {
        "status": "provisioned",
        "auth_mode": "Application Default Credentials (keyless)",
        "project_id": target_project,
        "dataset_id": dataset_id,
        "graph_name": graph_name,
        "sample_rows_loaded": load_sample,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision GCP Full Setup for Capital Markets Analyst")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project"))
    parser.add_argument("--dataset", default=os.environ.get("FSI_BQ_DATASET", "fsi_capital_markets"))
    parser.add_argument("--graph", default=os.environ.get("FSI_BQ_GRAPH_NAME", "disclosed_supply_chain_graph"))
    parser.add_argument("--spec", default="", help="Optional path to ./design-spec.md")
    parser.add_argument("--execute", action="store_true", help="Execute DDL against GCP via ADC")
    parser.add_argument(
        "--load-sample",
        action="store_true",
        help="Also replace the rows of sp500_companies and supply_chain_edges with the bundled sample book",
    )
    args = parser.parse_args()

    if args.spec and Path(args.spec).exists():
        _ = Path(args.spec).read_text(encoding="utf-8")

    result = run_setup(
        project_id=args.project,
        dataset_id=args.dataset,
        graph_name=args.graph,
        dry_run=not args.execute,
        load_sample=args.load_sample,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
