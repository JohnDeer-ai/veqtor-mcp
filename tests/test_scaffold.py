# SPDX-License-Identifier: Apache-2.0

from veqtor_docx import __version__ as docx_version
from veqtor_mcp import __version__ as mcp_version


def test_package_versions_match() -> None:
    assert docx_version == "0.0.0"
    assert mcp_version == docx_version

