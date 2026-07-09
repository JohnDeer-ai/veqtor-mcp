# SPDX-License-Identifier: Apache-2.0
"""Deterministic DOCX helpers for the Veqtor MCP toolchain."""

from .apply import ApplyError, apply_edits
from .extract import DocxError, extract_redlines
from .rounds import list_rounds
from .synthetic import generate_demo_rounds

__all__ = [
    "ApplyError",
    "DocxError",
    "__version__",
    "apply_edits",
    "extract_redlines",
    "generate_demo_rounds",
    "list_rounds",
]

__version__ = "0.0.0"
