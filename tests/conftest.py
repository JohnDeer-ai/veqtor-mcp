# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from veqtor_docx import generate_demo_rounds


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Synthetic negotiation rounds, generated once per test session."""
    out = tmp_path_factory.mktemp("demo-rounds")
    generate_demo_rounds(out)
    return out
