# SPDX-License-Identifier: Apache-2.0

import re
import tomllib
from pathlib import Path

from veqtor_docx import __version__ as docx_version
from veqtor_mcp import __version__ as mcp_version


ROOT = Path(__file__).parents[1]


def test_package_versions_match() -> None:
    assert docx_version == "0.1.0"
    assert mcp_version == docx_version


def test_api_validation_code_table_matches_the_runtime_contract() -> None:
    api = (ROOT / "API.md").read_text()

    assert (
        "| `delete_text` absent, `null`, empty, or non-string | "
        "`delete_text_missing` |"
    ) in api
    assert (
        "| `insert_text` present with a non-string value, including `null` | "
        "`invalid_edit` |"
    ) in api
    assert (
        "| `reinstate_text` present but not a non-empty string | `invalid_edit` |"
    ) in api
    assert "| Unknown edit or anchor field | `invalid_edit` |" in api


def test_current_release_changelog_is_status_neutral() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    releasing = (ROOT / "RELEASING.md").read_text()
    version = project["version"]
    marker = f"## {version}\n"

    assert changelog.count(marker) == 1
    section = changelog.split(marker, 1)[1].split("\n## ", 1)[0]
    assert f"Veqtor v{version} Alpha release contents." in section
    assert "Unreleased" not in section
    assert "Planned" not in section
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", section) is None
    assert "Publication dates are authoritative" in changelog
    assert "`published_at` timestamp" in changelog
    assert "only timeless release contents" in releasing
    assert "`published_at` timestamp" in releasing
