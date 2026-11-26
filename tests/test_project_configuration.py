"""Validate active project runtime declarations."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_active_project_targets_python_312() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    assert not (ROOT / "uv.lock").exists()
    assert "uv" not in pyproject.get("tool", {})
    assert pyproject["project"]["requires-python"] == ">=3.12"
    assert pyproject["tool"]["ruff"]["target-version"] == "py312"
    assert pyproject["tool"]["mypy"]["python_version"] == "3.12"

    github_ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    if github_ci_path.exists():
        github_ci = github_ci_path.read_text(encoding="utf-8")
        assert 'python-version: "3.12"' in github_ci

    azure_ci = (ROOT / "azure-pipelines.yml").read_text(encoding="utf-8")
    assert 'versionSpec: "3.12"' in azure_ci
    assert "ruff check src packages tests examples" in azure_ci
    assert "uv " not in azure_ci

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "uv " not in dockerfile
    assert "USER accelerator" in dockerfile
    assert "ENV ACCELERATOR_MODE=server" in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_hatchling_builds_pin_supported_tomlkit() -> None:
    manifests = [
        ROOT / "pyproject.toml",
        *sorted((ROOT / "packages").glob("*/pyproject.toml")),
        *sorted((ROOT / "examples" / "plugins").glob("*/pyproject.toml")),
    ]

    for manifest in manifests:
        pyproject = tomllib.loads(manifest.read_text(encoding="utf-8"))
        assert "tomlkit==0.15.2" in pyproject["build-system"]["requires"]

