"""Distribution metadata and genuine Home Assistant harness checks."""

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = (
    "pycarlink@git+https://github.com/angel12/PyCarLink.git@"
    "a11d551491fc8e6025c7d68314cc7666d040cbac"
)


def test_manifest_has_ui_setup():
    path = ROOT / "custom_components/carlink/manifest.json"
    assert path.is_file(), "Carlink manifest is missing"
    manifest = json.loads(path.read_text())
    assert list(manifest) == ["domain", "name", *sorted(set(manifest) - {"domain", "name"})]
    assert manifest == {
        "domain": "carlink",
        "name": "Carlink",
        "version": "0.1.1",
        "config_flow": True,
        "integration_type": "device",
        "iot_class": "cloud_polling",
        "codeowners": ["@angel12"],
        "documentation": "https://github.com/angel12/ha-carlink",
        "issue_tracker": "https://github.com/angel12/ha-carlink/issues",
        "requirements": [REQUIREMENT],
    }


def test_owner_approved_mit_license():
    license_text = (ROOT / "LICENSE").read_text()
    assert license_text.startswith("MIT License\n\nCopyright (c) 2026 angel12\n")
    assert "Permission is hereby granted, free of charge" in license_text
    assert "The above copyright notice and this permission notice" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in license_text
    assert "[MIT License](LICENSE)" in (ROOT / "README.md").read_text()


def test_single_component_directory():
    root = ROOT / "custom_components"
    assert root.is_dir(), "Custom components directory is missing"
    assert sorted(p.name for p in root.iterdir() if p.is_dir()
                  and not p.name.startswith((".", "__"))) == ["carlink"]


def test_hacs_metadata_matches_tested_baseline():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert hacs == {
        "name": "Carlink", "content_in_root": False, "homeassistant": "2026.9.3"
    }
    assert f"homeassistant=={hacs['homeassistant']}" in project["dependency-groups"]["test"]
    manifest = json.loads((ROOT / "custom_components/carlink/manifest.json").read_text())
    assert manifest["version"] == project["project"]["version"]
    assert manifest["requirements"] == project["project"]["dependencies"] == [REQUIREMENT]


def test_local_brand_is_valid_png_and_in_installable_component():
    from PIL import Image

    # Current HACS brands validator resolves relative to the integration content.
    installed = ROOT / "custom_components/carlink/brand/icon.png"
    assert installed.is_file(), "HACS needs the icon inside the integration"
    assert installed.read_bytes() == (ROOT / "brand/icon.png").read_bytes()
    with Image.open(installed) as image:
        assert image.format == "PNG"
        assert image.size == (256, 256)
        image.verify()


def test_ci_uses_baseline_offline_tests_and_unskipped_validators():
    import re

    import yaml

    # BaseLoader keeps the YAML 1.2 GitHub Actions `on` key a string.
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), yaml.BaseLoader)
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    steps = jobs["tests"]["steps"]
    setup = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "3.14.7"
    commands = "\n".join(s.get("run", "") for s in steps)
    for command in (
        "python -m pip install --group test",
        "python -m pip check",
        "python -m ruff check .",
        "python -m pytest -q --cov=custom_components.carlink --cov-report=term-missing",
    ):
        assert command in commands
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "--disable-socket" in config["tool"]["pytest"]["ini_options"]["addopts"]
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])
            assert "continue-on-error" not in step
    hacs = jobs["hacs"]["steps"][0]
    assert hacs["uses"].startswith("hacs/action@")
    assert hacs["with"] == {"category": "integration", "comment": "false"}
    assert any(s.get("uses", "").startswith("home-assistant/actions/hassfest@")
               for s in jobs["hassfest"]["steps"])


def test_installed_dependency_has_approved_provenance():
    from importlib.metadata import distribution

    installed = distribution("pycarlink")
    direct_url = installed.read_text("direct_url.json")
    assert direct_url is not None
    provenance = json.loads(direct_url)
    assert provenance["url"] == "https://github.com/angel12/PyCarLink.git"
    assert provenance["vcs_info"]["commit_id"] == REQUIREMENT.rsplit("@", 1)[1]
    assert provenance["vcs_info"]["requested_revision"] == REQUIREMENT.rsplit("@", 1)[1]
    assert "dir_info" not in provenance  # Not an adjacent editable checkout.


def test_fixtures_are_synthetic_not_http_recordings():
    import ast
    import re

    allowed_passwords = {
        "synthetic-secret", "new-synthetic-secret",
        "SYNTHETIC-SECRET", "NEW-SYNTHETIC-SECRET",
    }
    sources = list((ROOT / "tests").rglob("*.py"))
    assert sources
    for path in (ROOT / "tests").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert path.suffix == ".py", f"Review non-code fixture: {path}"
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "password":
                        assert isinstance(value, ast.Constant)
                        assert value.value in allowed_passwords, path
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            # Only literal email addresses, not requirement URLs / decorator names.
            emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", node.value)
            assert all(email.endswith(".invalid") for email in emails), path
            assert not re.search(r"(?i)(?:bearer|basic) [a-z0-9+/=_-]{12,}", node.value), path
            assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\n", node.value), path


@pytest.mark.parametrize(
    "fixture",
    [
        '{"password": "unapproved-test-value"}',
        '{"email": "synthetic@' + 'example.com"}',
        '{"header": "Bearer ' + 'NOTAREALTOKEN123456"}',
    ],
)
def test_fixture_guard_rejects_unsafe_examples(tmp_path, monkeypatch, fixture):
    """Negative controls prove the guard rejects unsanctioned fixture literals."""
    import sys

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "fixture.py").write_text(f"DATA = {fixture}\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        test_fixtures_are_synthetic_not_http_recordings()


async def test_real_home_assistant_harness(hass):
    from homeassistant.core import HomeAssistant
    from homeassistant.loader import async_get_integration

    from custom_components.carlink.const import DOMAIN, PLATFORMS

    assert isinstance(hass, HomeAssistant)
    assert await hass.async_add_executor_job(lambda: "executor works") == "executor works"
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == "carlink"
    assert integration.is_built_in is False
    assert integration.requirements == [REQUIREMENT]
    assert set(PLATFORMS) == {"lock", "button"}
