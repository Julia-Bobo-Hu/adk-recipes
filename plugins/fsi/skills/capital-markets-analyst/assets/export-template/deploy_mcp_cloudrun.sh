#!/usr/bin/env bash
# Deploy the Form 10-K Ripple Graph Streamable HTTP MCP App to Cloud Run for Gemini Enterprise.
#
# Run it from an exported bundle:
#
#   python3 scripts/export_app.py --output-dir ./dist/capital-markets-app
#   cd ./dist/capital-markets-app
#   GOOGLE_CLOUD_PROJECT=<project-id> bash deploy_mcp_cloudrun.sh
#
# Settings (environment variables):
#   GOOGLE_CLOUD_PROJECT         required
#   FSI_RUN_REGION               Cloud Run region, default us-central1
#   FSI_MCP_SERVICE_NAME         default fsi-ripple-mcp-app
#   FSI_MCP_RUNTIME_SA           runtime service account name, default fsi-ripple-mcp-sa
#   FSI_SHEET_ID                 optional shared Google Sheet ID for in-widget / chat Sheet sync
#   FSI_SHEETS_SERVICE_ACCOUNT   optional SA email to impersonate for Google Sheets API access
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT first}"
REGION="${FSI_RUN_REGION:-us-central1}"
SERVICE_NAME="${FSI_MCP_SERVICE_NAME:-fsi-ripple-mcp-app}"
RUNTIME_SA_NAME="${FSI_MCP_RUNTIME_SA:-fsi-ripple-mcp-sa}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

if [[ ! -f Dockerfile || ! -f uv.lock || ! -d scripts ]]; then
  echo "Run this from an exported bundle (python3 scripts/export_app.py --output-dir <dir>)." >&2
  exit 2
fi

if ! gcloud iam service-accounts describe "${RUNTIME_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="FSI 10-K Ripple Graph MCP App SA" \
    --quiet
fi

ENV_PAIRS=()
if [[ -n "${FSI_SHEET_ID:-}" ]]; then
  ENV_PAIRS+=("FSI_SHEET_ID=${FSI_SHEET_ID}")
fi
if [[ -n "${FSI_SHEETS_SERVICE_ACCOUNT:-}" ]]; then
  ENV_PAIRS+=("FSI_SHEETS_SERVICE_ACCOUNT=${FSI_SHEETS_SERVICE_ACCOUNT}")
fi
ENV_ARG=()
if [[ ${#ENV_PAIRS[@]} -gt 0 ]]; then
  IFS=, eval 'JOINED="${ENV_PAIRS[*]}"'
  ENV_ARG=(--set-env-vars="${JOINED}")
fi

gcloud run deploy "${SERVICE_NAME}" \
  --source=. \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${RUNTIME_SA}" \
  --command="python3" \
  --args="scripts/mcp_server.py,--transport,streamable-http,--host,0.0.0.0,--port,8080,--full-suite" \
  --no-allow-unauthenticated \
  --no-cpu-throttling \
  --min-instances=1 \
  --max-instances=4 \
  --memory=1Gi \
  "${ENV_ARG[@]}" \
  --quiet

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
DE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${DE_SA}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null || true

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo "Deployed Streamable HTTP MCP App: ${SERVICE_URL}/mcp"
