# SPDX-License-Identifier: Apache-2.0

import json
import re
import tomllib
from pathlib import Path

from veqtor_docx import __version__ as docx_version
from veqtor_mcp import __version__ as mcp_version


ROOT = Path(__file__).parents[1]


def test_package_versions_match() -> None:
    assert docx_version == "0.1.1"
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


def test_docs_disambiguate_reinstate_and_compact_clause_digest() -> None:
    api = (ROOT / "API.md").read_text()
    limitations = (ROOT / "KNOWN_LIMITATIONS.md").read_text()

    assert re.search(r"This is not Word Reject: Veqtor does\s+not accept", api)
    assert "It is never a digest of the clause body" in api
    assert re.search(
        r"does not perform Word Reject and does not\s+remove", limitations
    )
    assert "observation is not side-effect free" in limitations


def test_export_example_matches_compact_count_and_gap_contract() -> None:
    api = (ROOT / "API.md").read_text()
    export_section = api.split("## `export_decision_record`", 1)[1].split(
        "### Canonical JSON v1", 1
    )[0]
    json_blocks = re.findall(r"```json\n(.*?)\n```", export_section, re.DOTALL)
    assert len(json_blocks) == 2
    output = json.loads(json_blocks[1])

    assert output["returned_count"] == len(output["records"])
    assert output["total_count"] == 1
    assert output["access_count"] == 3
    assert output["truncated"] is False
    assert output["current_export_event"]["record_id"] == output["record_id"]
    expected_current_number = output["total_count"] + output["access_count"] + 1
    assert output["record_id"] == f"dr_{expected_current_number:03d}"

    match = output["records"][0]["result"]["matches"]["sample"][0]
    assert set(match) == {
        "part_name",
        "revision_ids",
        "side",
        "clause_sha256",
    }
    assert match["part_name"] == "word/document.xml"
    assert match["clause_sha256"] is None


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
