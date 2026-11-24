#!/usr/bin/env bash
# Deploy the Capital Markets Analyst workstation to Cloud Run with IAM-only access.
#
# Run it from an exported bundle, never from the skill folder itself:
#
#   python3 scripts/export_app.py --output-dir ./dist/capital-markets-app
#   cd ./dist/capital-markets-app
#   GOOGLE_CLOUD_PROJECT=<project-id> bash deploy_cloudrun.sh
#
# What it creates (all idempotent):
#   - a dedicated runtime service account with roles/aiplatform.user only, so the service
#     can call Gemini for "Run live agent eval" and nothing else;
#   - one Cloud Run service that only IAM-authorised callers can open, one instance;
#   - only if FSI_BUILD_SA is set: a dedicated build service account with roles/run.builder.
#
# Settings (environment variables):
#   GOOGLE_CLOUD_PROJECT   required
#   FSI_RUN_REGION         Cloud Run region, default us-central1
#   GEMINI_LOCATION        where Gemini is called, default global. It is deliberately
#                          separate from the Cloud Run region.
#   GEMINI_MODEL           optional agent model override
#   FSI_JUDGE_MODEL        optional rubric-judge model override
#   FSI_SERVICE_NAME       default fsi-capital-markets-analyst
#   FSI_RUNTIME_SA         runtime service account name, default fsi-workstation
#   FSI_MIN_INSTANCES      default 1. The live evaluation result and the sign-off log live
#                          in the instance, so 0 loses them whenever the service idles.
#   FSI_INVOKER_MEMBER     optional, e.g. user:analyst@example.com: who may open the service
#   FSI_BUILD_SA           optional name of a build service account, e.g. fsi-workstation-build.
#                          Set it when the build fails with "does not have
#                          storage.objects.get access": the project's default build identity
#                          then has no roles.
#
# Open the private service from a workstation with gcloud signed in as an invoker:
#   gcloud run services proxy "$FSI_SERVICE_NAME" --project <project-id> --region <region> --port 8080
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT first}"
REGION="${FSI_RUN_REGION:-us-central1}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
SERVICE_NAME="${FSI_SERVICE_NAME:-fsi-capital-markets-analyst}"
RUNTIME_SA_NAME="${FSI_RUNTIME_SA:-fsi-workstation}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
MIN_INSTANCES="${FSI_MIN_INSTANCES:-1}"

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

if [[ ! -f Dockerfile || ! -f uv.lock || ! -d scripts ]]; then
  echo "Run this from an exported bundle (python3 scripts/export_app.py --output-dir <dir>)." >&2
  exit 2
fi

# Create a service account unless it exists.
ensure_service_account() {
  local name="$1" display_name="$2"
  local email="${name}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "${email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Creating service account ${email}..."
    gcloud iam service-accounts create "${name}" --project="${PROJECT_ID}" --display-name="${display_name}" --quiet
  fi
}

# Grant a project role. A new service account can take a few seconds to become visible to
# IAM, so the grant is retried for up to half a minute before the error is shown.
grant_project_role() {
  local member="$1" role="$2" attempt output=""
  echo "Granting ${role} to ${member}..."
  for attempt in 1 2 3 4 5 6; do
    if output="$(gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="${member}" --role="${role}" --condition=None --quiet 2>&1)"; then
      return 0
    fi
    sleep 5
  done
  echo "${output}" >&2
  return 1
}

ensure_service_account "${RUNTIME_SA_NAME}" "FSI Capital Markets workstation (Cloud Run runtime)"
grant_project_role "serviceAccount:${RUNTIME_SA}" "roles/aiplatform.user"

ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=True,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GEMINI_LOCATION}"
if [[ -n "${GEMINI_MODEL:-}" ]]; then ENV_VARS+=",GEMINI_MODEL=${GEMINI_MODEL}"; fi
if [[ -n "${FSI_JUDGE_MODEL:-}" ]]; then ENV_VARS+=",FSI_JUDGE_MODEL=${FSI_JUDGE_MODEL}"; fi
if [[ -n "${FSI_REVIEWER_MODEL:-}" ]]; then ENV_VARS+=",FSI_REVIEWER_MODEL=${FSI_REVIEWER_MODEL}"; fi
if [[ -n "${FSI_SHEET_ID:-}" ]]; then ENV_VARS+=",FSI_SHEET_ID=${FSI_SHEET_ID}"; fi

# --timeout 900: one live evaluation is a single request of about five and a half minutes.
# --max-instances 1: the evaluation result and the sign-off log are per instance.
DEPLOY_ARGS=(
  --project="${PROJECT_ID}"
  --region="${REGION}"
  --source=.
  --no-allow-unauthenticated
  --service-account="${RUNTIME_SA}"
  --min-instances="${MIN_INSTANCES}"
  --max-instances=1
  --cpu=1
  --memory=1Gi
  --timeout=900
  --set-env-vars="${ENV_VARS}"
  --quiet
)
if [[ -n "${FSI_BUILD_SA:-}" ]]; then
  BUILD_SA="${FSI_BUILD_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
  ensure_service_account "${FSI_BUILD_SA}" "FSI Capital Markets workstation (image build)"
  grant_project_role "serviceAccount:${BUILD_SA}" "roles/run.builder"
  DEPLOY_ARGS+=(--build-service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}")
fi

echo "Deploying ${SERVICE_NAME} to project=${PROJECT_ID} region=${REGION} (private, Gemini location ${GEMINI_LOCATION})..."
gcloud run deploy "${SERVICE_NAME}" "${DEPLOY_ARGS[@]}"

INVOKER_GRANT_FAILED=0
if [[ -n "${FSI_INVOKER_MEMBER:-}" ]]; then
  echo "Granting roles/run.invoker on ${SERVICE_NAME} to ${FSI_INVOKER_MEMBER}..."
  if ! gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="${FSI_INVOKER_MEMBER}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1; then
    INVOKER_GRANT_FAILED=1
    echo "Could not grant roles/run.invoker to ${FSI_INVOKER_MEMBER}: the deployer needs roles/run.admin for that." >&2
    echo "Project owners and editors can open the service without this grant." >&2
  fi
fi

echo "Deployed. Open it with:"
echo "  gcloud run services proxy ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --port 8080"
echo "If gcloud cannot install its proxy component, run this from this folder instead:"
echo "  python3 scripts/cloudrun_proxy.py --project ${PROJECT_ID} --region ${REGION} --service ${SERVICE_NAME} --port 8080"
# The service is deployed either way; a refused invoker grant is reported with exit code 3.
if [[ "${INVOKER_GRANT_FAILED}" == "1" ]]; then exit 3; fi
