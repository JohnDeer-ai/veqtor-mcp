# SPDX-License-Identifier: Apache-2.0
"""Frozen v0.2 release inventory plus the current development runtime ratchet."""

from __future__ import annotations


PROJECT_NAME = "veqtor-mcp"
DIST_NAME = "veqtor_mcp"
VERSION = "0.2.0"
WHEEL_FILENAME = f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
SDIST_FILENAME = f"{DIST_NAME}-{VERSION}.tar.gz"
MCPB_FILENAME = f"{PROJECT_NAME}-{VERSION}-macos.mcpb"
CHECKSUMS_FILENAME = "SHA256SUMS.txt"
BUILD_PYTHON_VERSION = "3.12.13"
BUILD_UV_VERSION = "0.11.28"
BUILD_HATCHLING_VERSION = "1.31.0"
BUILD_NODE_VERSION = "22.17.0"
MCPB_CLI_PACKAGE = "@anthropic-ai/mcpb"
MCPB_CLI_VERSION = "2.1.2"
MCPB_MANIFEST_VERSION = "0.4"
MCPB_PLATFORM = "darwin"
SOURCE_DATE_EPOCH = "1580601600"
CANONICAL_GZIP_XFL = 2
CANONICAL_GZIP_OS = 255
CANONICAL_ZIP_VERSION_MADE = 788  # Unix (3) + ZIP specification 2.0 (20)
CANONICAL_ZIP_VERSION_NEEDED = 20
CANONICAL_ZIP_FLAGS = 0
CANONICAL_ZIP_METHOD = 8
CANONICAL_ZIP_TIME = 0
CANONICAL_ZIP_DATE = 20546  # 2020-02-02 in MS-DOS date form
CANONICAL_TAR_MODE = 0o644
FIVE_EDIT_OUTPUT_SHA256 = (
    "123771a24f4a3f7e3ae6e9e4785c1e5ebd10edb9923ddcec8dcc0d340f886c41"
)

MAX_ARCHIVE_FILE_BYTES = 16 * 1_048_576
MAX_ARCHIVE_EXPANDED_BYTES = 16 * 1_048_576
MAX_ARCHIVE_MEMBER_BYTES = 4 * 1_048_576
MAX_ARCHIVE_MEMBERS = 128
MAX_ARCHIVE_METADATA_BYTES = 256 * 1_024
MAX_ARCHIVE_MEMBER_NAME_BYTES = 1_024
MAX_NORMALIZATION_PASSES = 8

RUNTIME_SOURCE_FILES = (
    "src/veqtor_docx/__init__.py",
    "src/veqtor_docx/_ooxml.py",
    "src/veqtor_docx/apply.py",
    "src/veqtor_docx/contracts.py",
    "src/veqtor_docx/extract.py",
    "src/veqtor_docx/rounds.py",
    "src/veqtor_docx/synthetic.py",
    "src/veqtor_docx/verify.py",
    "src/veqtor_mcp/__init__.py",
    "src/veqtor_mcp/contracts.py",
    "src/veqtor_mcp/records.py",
    "src/veqtor_mcp/server.py",
)

# Current development packaging is ratcheted separately from the immutable
# v0.2 release/MCPB inventory above.  A future frozen release contract must
# promote this complete set deliberately rather than mutating v0.2 in place.
DEVELOPMENT_RUNTIME_SOURCE_FILES = (
    "src/veqtor_docx/__init__.py",
    "src/veqtor_docx/_ooxml.py",
    "src/veqtor_docx/apply.py",
    "src/veqtor_docx/contracts.py",
    "src/veqtor_docx/extract.py",
    "src/veqtor_docx/inspect.py",
    "src/veqtor_docx/rounds.py",
    "src/veqtor_docx/synthetic.py",
    "src/veqtor_docx/verify.py",
    "src/veqtor_mcp/__init__.py",
    "src/veqtor_mcp/_inspection_live.py",
    "src/veqtor_mcp/contracts.py",
    "src/veqtor_mcp/records.py",
    "src/veqtor_mcp/server.py",
)

MCPB_REQUIRED_TOOLS = (
    "list_rounds",
    "extract_redlines",
    "verify_quote",
    "preflight_edits",
    "apply_edits",
    "export_decision_record",
)
MCPB_DEMO_FILENAMES = (
    "round-1-outgoing-draft.docx",
    "round-2-counterparty-redline.docx",
    "round-3-our-counter.docx",
    "round-4-counterparty-reply.docx",
)
MCPB_SOURCE_MAP = {
    "manifest.json": "packaging/mcpb/manifest.json",
    "README.md": "packaging/mcpb/README.md",
    "demo/README.md": "packaging/mcpb/demo/README.md",
    "demo/FIRST_PROMPT.txt": "packaging/mcpb/demo/FIRST_PROMPT.txt",
    "icon.png": "website/public/logo-512.png",
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    "pyproject.toml": "pyproject.toml",
    "uv.lock": "uv.lock",
    **{source: source for source in RUNTIME_SOURCE_FILES},
}
MCPB_GENERATED_MEMBERS = frozenset(
    f"demo/{filename}" for filename in MCPB_DEMO_FILENAMES
)
MCPB_MEMBERS = frozenset((*MCPB_SOURCE_MAP, *MCPB_GENERATED_MEMBERS))
GITHUB_PAYLOAD_FILENAMES = (
    WHEEL_FILENAME,
    SDIST_FILENAME,
    MCPB_FILENAME,
)
GITHUB_RELEASE_FILENAMES = (*GITHUB_PAYLOAD_FILENAMES, CHECKSUMS_FILENAME)

PUBLIC_DOCUMENT_FILES = (
    ".gitignore",
    "API.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "KNOWN_LIMITATIONS.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "RELEASING.md",
    "ROADMAP.md",
    "SECURITY.md",
    "TRADEMARKS.md",
    "pyproject.toml",
)

SDIST_GIT_FILES = frozenset((*PUBLIC_DOCUMENT_FILES, *RUNTIME_SOURCE_FILES))
WHEEL_SOURCE_MAP = {
    source.removeprefix("src/"): source for source in RUNTIME_SOURCE_FILES
}

DIST_INFO_DIR = f"{DIST_NAME}-{VERSION}.dist-info"
WHEEL_GENERATED_MEMBERS = frozenset(
    {
        f"{DIST_INFO_DIR}/METADATA",
        f"{DIST_INFO_DIR}/WHEEL",
        f"{DIST_INFO_DIR}/entry_points.txt",
        f"{DIST_INFO_DIR}/RECORD",
    }
)
WHEEL_LICENSE_MAP = {
    f"{DIST_INFO_DIR}/licenses/LICENSE": "LICENSE",
    f"{DIST_INFO_DIR}/licenses/NOTICE": "NOTICE",
}
WHEEL_MEMBERS = frozenset(
    (*WHEEL_SOURCE_MAP, *WHEEL_GENERATED_MEMBERS, *WHEEL_LICENSE_MAP)
)

SDIST_ROOT = f"{DIST_NAME}-{VERSION}"
SDIST_GENERATED_MEMBERS = frozenset({f"{SDIST_ROOT}/PKG-INFO"})
SDIST_SOURCE_MAP = {f"{SDIST_ROOT}/{source}": source for source in SDIST_GIT_FILES}
SDIST_MEMBERS = frozenset((*SDIST_SOURCE_MAP, *SDIST_GENERATED_MEMBERS))

EXPECTED_ENTRY_POINTS = """[console_scripts]
veqtor-demo-rounds = veqtor_docx.synthetic:main
veqtor-mcp = veqtor_mcp.server:main
"""

EXPECTED_WHEEL_METADATA = f"""Wheel-Version: 1.0
Generator: hatchling {BUILD_HATCHLING_VERSION}
Root-Is-Purelib: true
Tag: py3-none-any
"""

RELEASE_TITLE = f"Veqtor v{VERSION} Alpha"
RELEASE_NOTES_PATH = f".github/release-notes/v{VERSION}.md"
