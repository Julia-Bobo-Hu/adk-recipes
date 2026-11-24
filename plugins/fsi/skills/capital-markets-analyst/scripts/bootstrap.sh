#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SKILL_DIR}"

if [[ -x "${SKILL_DIR}/.venv/bin/python" ]]; then
  PY_BIN="${SKILL_DIR}/.venv/bin/python"
elif [[ -x "${SKILL_DIR}/../../../../.venv/bin/python" ]]; then
  PY_BIN="${SKILL_DIR}/../../../../.venv/bin/python"
else
  PY_BIN="${PYTHON:-python3}"
fi

echo "[fsi-capital-markets-analyst] Verifying deterministic Form 10-K + XBRL evaluation gates..."
"${PY_BIN}" scripts/eval_runner.py --no-save >/dev/null
"${PY_BIN}" scripts/model_exporter.py --output "${SKILL_DIR}/_output/sp500_read_the_fine_print_model.xlsx"
echo "[fsi-capital-markets-analyst] Quick Start ready (<60s). Run: ${PY_BIN} scripts/server.py --port 8080"
