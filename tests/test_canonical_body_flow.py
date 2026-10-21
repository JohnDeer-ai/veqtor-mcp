# SPDX-License-Identifier: Apache-2.0
"""Synthetic regressions for property metadata versus rendered body content."""

from __future__ import annotations

import pytest
from lxml import etree

from veqtor_docx._ooxml import (
    W_NS,
    canonical_body_flow_v1,
    canonical_run_text,
)


def _body(properties: str) -> etree._Element:
    return etree.fromstring(
        f'<w:body xmlns:w="{W_NS}" xmlns:x="urn:synthetic:unknown">'
        f"<w:p>{properties}<w:r><w:t>Left</w:t><w:tab/><w:t>Right</w:t></w:r>"
        "</w:p><w:p/></w:body>"
    )


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("tab_kind", ["left", "right", "decimal", "clear"])
def test_paragraph_tab_stops_are_properties_not_rendered_payload(historical, tab_kind):
    properties = (
        f'<w:pPr><w:tabs><w:tab w:val="{tab_kind}" w:pos="1440"/>'
        "</w:tabs></w:pPr>"
    )
    if historical:
        properties = (
            '<w:pPr><w:pPrChange w:id="7" w:author="Synthetic Reviewer">'
            f"{properties}</w:pPrChange></w:pPr>"
        )
    body = _body(properties)
    flow = canonical_body_flow_v1(body)

    assert flow.container_policy["coverage_complete"] is True
    assert flow.container_policy["excluded_by_kind"] == {}
    assert flow.container_policy["legacy_two_field_anchor_safe"] is True
    assert [p.paragraph_index for p in flow.paragraphs] == [0, 1]
    assert [canonical_run_text(p.element) for p in flow.paragraphs] == [
        "Left\tRight", "",
    ]
    assert etree.tostring(body) == etree.tostring(_body(properties))


@pytest.mark.parametrize("properties", [
    # A tab is metadata only in the direct pPr/tabs/tab shape.
    "<w:pPr><w:tab/></w:pPr>",
    "<w:rPr><w:tabs><w:tab/></w:tabs></w:rPr>",
    "<w:pPr><x:wrapper><w:tabs><w:tab/></w:tabs></x:wrapper></w:pPr>",
    "<w:pPr><w:tabs><x:wrapper><w:tab/></x:wrapper></w:tabs></w:pPr>",
    # Recognizing a tab stop must not prune illegal payload under or beside it.
    '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440">'
    "<w:r><w:t>Hidden</w:t></w:r></w:tab></w:tabs></w:pPr>",
    '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/>'
    "<x:wrapper><w:r><w:t>Hidden</w:t></w:r></x:wrapper></w:tabs></w:pPr>",
    '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/>'
    '<w:ins w:id="8" w:author="Synthetic Reviewer"/></w:tabs></w:pPr>',
    '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/>'
    "<w:p/></w:tabs></w:pPr>",
])
def test_tab_stop_exception_does_not_hide_unknown_or_illegal_payload(properties):
    flow = canonical_body_flow_v1(_body(properties))

    assert flow.container_policy["coverage_complete"] is False
    assert flow.container_policy["excluded_by_kind"] == {"unknown_container": 1}
    assert flow.container_policy["legacy_two_field_anchor_safe"] is False
    assert [canonical_run_text(p.element) for p in flow.paragraphs] == [
        "Left\tRight", "",
    ]


def test_unknown_inline_wrapper_remains_excluded_beside_valid_tab_stops():
    body = _body(
        '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/></w:tabs></w:pPr>'
        "<x:wrapper><w:r><w:t>Hidden</w:t></w:r></x:wrapper>"
    )
    flow = canonical_body_flow_v1(body)

    assert flow.container_policy["excluded_by_kind"] == {"unknown_container": 1}
    assert flow.excluded_subtrees[0].element.tag == "{urn:synthetic:unknown}wrapper"
    assert canonical_run_text(flow.paragraphs[0].element) == "Left\tRight"
