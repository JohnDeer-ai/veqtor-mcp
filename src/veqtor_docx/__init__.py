# SPDX-License-Identifier: Apache-2.0
"""Deterministic DOCX helpers for the Veqtor MCP toolchain."""

from .apply import ApplyError, apply_edits, preflight_edits
from .extract import DocxError, extract_redlines
from .inspect import InspectError, inspect_document
from .rounds import RoundError, list_rounds
from .synthetic import SyntheticError, generate_demo_rounds
from .verify import VerifyError, verify_quote

__all__ = [
    "ApplyError",
    "DocxError",
    "InspectError",
    "RoundError",
    "SyntheticError",
    "VerifyError",
    "__version__",
    "apply_edits",
    "extract_redlines",
    "generate_demo_rounds",
    "list_rounds",
    "inspect_document",
    "preflight_edits",
    "verify_quote",
]

__version__ = "0.3.0"
