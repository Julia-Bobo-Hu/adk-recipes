# Google Cloud ADK Recipes — Financial Services (`plugins/fsi`)

An Agent Plugin for Financial Services on Google Cloud, built on the Agent Development Kit (ADK), Vertex AI Gemini and BigQuery (SQL and the ISO `GQL` Property Graph).

## Included skills

| Skill | Directory | Install Command | Summary |
| :--- | :--- | :--- | :--- |
| **`fsi-capital-markets-analyst`** | `skills/capital-markets-analyst/` | `npx skills add google/adk-recipes/plugins/fsi --skill fsi-capital-markets-analyst` | Dedicated **SEC Form 10-K & XBRL** skill for capital-markets analysts. Reads reported annual figures from SEC XBRL company facts with growth and margins computed in `Decimal` arithmetic (for 20 bundled issuers offline and any US GAAP Form 10-K filer when live lookups are on), scales disclosed Item 7A sensitivities to rate/oil scenarios, traces 1st-to-3rd-order customer/supplier contagion (Python and BigQuery `GQL`), syncs live-formula models and peer comps to `.xlsx` or a shared Google Sheet, audits figures and unsourced claims with a second model, and gates release behind a 27-case evaluation and maker-checker sign-off. |

## Quick Commands

```bash
make test     # Offline test suite
make demo     # Local Capital Markets Form 10-K workstation on http://127.0.0.1:8080
```

## MCP App server (`stdio` & Streamable HTTP)

`mcp.json` declares `capital-markets-ripple` (`skills/capital-markets-analyst/scripts/mcp_server.py`) over `stdio` for IDE hosts, and the same script serves Streamable HTTP (`--transport streamable-http --full-suite`, `deploy_mcp_cloudrun.sh`) for Cloud Run and Gemini Enterprise Custom MCP Server connectors. In an MCP Apps host it renders the interactive 10-K supply-chain ripple graph inside the conversation (verified in Gemini Enterprise on 2026-11-23) with optional one-click Google Sheet sync; elsewhere it returns a text summary.

See [`skills/capital-markets-analyst/README.md`](skills/capital-markets-analyst/README.md) for the sample data, the evaluation tiers and the current limits.
