# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from veqtor_docx import generate_demo_rounds


def _required_round_map_nodeids(items: list[pytest.Item]) -> set[str]:
    for item in items:
        mapping = getattr(item.module, "ACCEPTANCE_CLAUSE_NODEIDS", None)
        if mapping is not None:
            return {
                nodeid
                for fixture_nodeids in mapping.values()
                for nodeid in fixture_nodeids
            }
    return set()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    required = _required_round_map_nodeids(items)
    for item in items:
        if item.nodeid.split("[", 1)[0] in required:
            setattr(item, "_round_map_required_acceptance", True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if not getattr(item, "_round_map_required_acceptance", False):
        return
    if report.skipped or getattr(report, "wasxfail", None) is not None:
        report.outcome = "failed"
        report.longrepr = (
            f"required Round Map acceptance evidence did not execute: {item.nodeid}"
        )


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Synthetic negotiation rounds, generated once per test session."""
    out = tmp_path_factory.mktemp("demo-rounds")
    generate_demo_rounds(out)
    return out
