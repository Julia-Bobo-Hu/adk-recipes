"""Export self-contained Cloud Run and Gemini Enterprise A2A deployment bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine  # noqa: E402


def export_bundle(output_dir: Path) -> Path:
    """Copy skill runtime and emit `agents-cli-manifest.yaml` in the target build directory only."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    root = fsi_engine.SKILL_ROOT
    # uv.lock travels with the bundle: the Dockerfile installs exactly the locked versions.
    for rel in ("pyproject.toml", "uv.lock", "README.md", "EVAL.yaml", "SKILL.md"):
        shutil.copy2(root / rel, output_dir / rel)
    for folder in ("scripts", "references", "assets"):
        shutil.copytree(root / folder, output_dir / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for tmpl in (root / "assets" / "export-template").iterdir():
        shutil.copy2(tmpl, output_dir / tmpl.name)

    manifest_yaml = """name: fsi-capital-markets-analyst
version: "1.0.0"
entrypoint: scripts.agent:root_agent
runtime: python312
auth:
  mode: application_default_credentials
  allow_unauthenticated_ingress: false
"""
    (output_dir / "agents-cli-manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Cloud Run / Gemini Enterprise deployment bundle")
    parser.add_argument("--output-dir", default=str(fsi_engine.SKILL_ROOT / "_output" / "deploy_bundle"))
    args = parser.parse_args()
    out = export_bundle(Path(args.output_dir))
    print(f"Exported deployment bundle to {out}")


if __name__ == "__main__":
    main()
