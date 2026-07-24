# SPDX-License-Identifier: Apache-2.0

import json
import re
import tomllib
from pathlib import Path

from veqtor_docx import __version__ as docx_version
from veqtor_mcp import __version__ as mcp_version


ROOT = Path(__file__).parents[1]


def test_package_versions_match() -> None:
    assert docx_version == "0.3.0"
    assert mcp_version == docx_version


def test_api_validation_code_table_matches_the_runtime_contract() -> None:
    api = (ROOT / "API.md").read_text()

    assert (
        "| `delete_text` absent, `null`, empty, or non-string | `delete_text_missing` |"
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
    assert re.search(r"does not perform Word Reject and does not\s+remove", limitations)
    assert "observation is not side-effect free" in limitations


def test_docs_describe_the_actual_bounded_zip_boundary() -> None:
    api = (ROOT / "API.md").read_text()
    limitations = (ROOT / "KNOWN_LIMITATIONS.md").read_text()
    security = (ROOT / "SECURITY.md").read_text()

    assert "only `STORED` or `DEFLATED`" in limitations
    assert re.search(r"Standard\s+32-bit data descriptors", limitations)
    assert "actual output and CRC" in api
    assert "DEFLATED data uses bounded input and output chunks" in api
    assert "STORED data is a\nbounded direct span" in api
    assert "500 MiB of aggregate actual expanded output" in api
    assert re.search(r"DEFLATED decoder output and STORED direct-span bytes", api)
    assert re.search(r"returns no partial round list", api)
    assert re.search(
        r"container preflight before any\s+member-output processing", limitations
    )
    assert "Exceeding the shared scan budget" in limitations
    assert re.search(r"one cumulative 500 MiB actual expanded-output budget", security)
    assert re.search(r"STORED direct-span bytes remain charged", security)
    assert re.search(r"rejected\s+by a later CRC, XML or required-part", security)
    assert re.search(r"beyond the budget fails the complete call", security)
    assert "non-ZIP64 local and central extra fields may differ" in api
    assert re.search(r"exact DEFLATE end of\s+stream", api)
    assert re.search(r"no package member is\s+trusted solely", security)
    for code in (
        "resource_limit_exceeded",
        "unsupported_compression",
        "encrypted_docx",
        "file_unextractable",
    ):
        assert f"`{code}`" in api


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


def test_changelog_keeps_timeless_release_copy() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    releasing = (ROOT / "RELEASING.md").read_text()
    version = project["version"]
    release_marker = f"## {version}\n"

    assert changelog.count(release_marker) == 1
    release = changelog.split(release_marker, 1)[1].split("\n## ", 1)[0]
    assert "Veqtor v0.3.0 Alpha release contents." in release
    assert "Unreleased" not in release
    assert "Planned" not in release
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", release) is None
    assert "Publication dates are authoritative" in changelog
    assert "`published_at` timestamp" in changelog
    assert "only timeless release contents" in releasing
    assert "`published_at` timestamp" in releasing


def test_release_copy_is_promotion_safe_and_site_stays_on_published_version() -> None:
    readme = (ROOT / "README.md").read_text()
    immutable_docs = "\n".join(
        (ROOT / name).read_text()
        for name in ("README.md", "API.md", "KNOWN_LIMITATIONS.md", "ROADMAP.md")
    )
    setup = (ROOT / "website" / "src" / "pages" / "setup.astro").read_text()
    public_pages = "\n".join(
        path.read_text()
        for path in (ROOT / "website" / "src" / "pages").rglob("*.astro")
    )
    releasing = (ROOT / "RELEASING.md").read_text()
    assert (
        "claude mcp add --transport stdio --scope user veqtor -- uvx veqtor-mcp@X.Y.Z"
    ) in readme
    assert (
        "claude mcp add --transport stdio --scope user veqtor "
        '-e VEQTOR_TRACKED_CHANGE_AUTHOR="Your Name" -- '
        "uvx veqtor-mcp@X.Y.Z"
    ) in readme
    assert '"args": ["veqtor-mcp@0.1.2"]' in setup
    assert "https://pypi.org/project/veqtor-mcp/" in readme
    assert "Both sources expose `0.3.0`" in readme
    assert "| Otherwise | `0.1.2` |" in readme
    assert "current public distribution" not in immutable_docs.lower()
    assert "current public release" not in immutable_docs.lower()
    assert "v0.3.0 is not public yet" in setup
    for forbidden in (
        "releases/tag/v0.3.0",
        "releases/download/v0.3.0",
    ):
        assert forbidden not in readme
        assert forbidden not in public_pages
    assert "state-neutral version-selection" in releasing
    assert re.search(
        r"must activate the public `v0\.3\.0` links.*deploy them, and\s+smoke the live setup page",
        releasing,
        re.DOTALL,
    )


def test_pypi_long_description_has_no_repository_relative_links() -> None:
    readme = (ROOT / "README.md").read_text()
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)

    assert targets
    assert all(target.startswith(("https://", "#")) for target in targets)
