# SPDX-License-Identifier: Apache-2.0
"""Acceptance coverage for the internal exact adjacent history resolver."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace

import pytest
from lxml import etree

from veqtor_docx._ooxml import W_NS, canonical_body_flow_v1, w
from veqtor_docx._projection import build_paragraph_projection_v1
from veqtor_docx.inspect import (
    _Paragraph,
    _Snapshot,
    _accepted_current_text,
    _load_snapshot_from_payload,
    _paragraph_ref,
    _sections,
    _sha256_text,
)
from veqtor_mcp import _history_resolution as history
from veqtor_mcp._history_resolution import (
    HistoryResolutionError,
    ParagraphHistoryObservation,
    ParagraphHistorySeed,
    resolve_paragraph_history,
)


@dataclass(frozen=True)
class _ParagraphSpec:
    current: str
    rejected: str | None = None
    unavailable: bool = False
    heading: bool = False


def _run(parent: etree._Element, text: str, *, deleted: bool = False) -> None:
    run = etree.SubElement(parent, w("r"))
    atom = etree.SubElement(run, w("delText") if deleted else w("t"))
    atom.text = text


def _paragraph(parent: etree._Element, spec: _ParagraphSpec) -> None:
    paragraph = etree.SubElement(parent, w("p"))
    if spec.heading:
        properties = etree.SubElement(paragraph, w("pPr"))
        outline = etree.SubElement(properties, w("outlineLvl"))
        outline.set(w("val"), "0")
    if spec.rejected is None:
        _run(paragraph, spec.current)
    else:
        deletion = etree.SubElement(paragraph, w("del"))
        _run(deletion, spec.rejected, deleted=True)
        insertion = etree.SubElement(paragraph, w("ins"))
        _run(insertion, spec.current)
    if spec.unavailable:
        _run(paragraph, "unattributed deleted text", deleted=True)


def _observation(
    name: str,
    specs: list[_ParagraphSpec],
    *,
    file_identity: str | None = None,
) -> ParagraphHistoryObservation:
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    for spec in specs:
        _paragraph(body, spec)
    flow = canonical_body_flow_v1(body)
    paragraphs = []
    for item in flow.paragraphs:
        text, has_tracked = _accepted_current_text(item.element)
        paragraphs.append(
            _Paragraph(
                element=item.element,
                paragraph_index=item.paragraph_index,
                container_kind=item.container_kind,
                text=text,
                text_sha256=_sha256_text(text),
                has_tracked_text_revisions=has_tracked,
            )
        )
    frozen_paragraphs = tuple(paragraphs)
    sections, section_by_paragraph = _sections(frozen_paragraphs, {}, None)
    file_key = file_identity if file_identity is not None else name
    file_sha256 = hashlib.sha256(file_key.encode("utf-8")).hexdigest()
    snapshot = _Snapshot(
        path=f"/descriptor-captured/{name}.docx",
        file_sha256=file_sha256,
        body_flow=flow,
        body_xml=etree.tostring(body, with_tail=False),
        paragraphs=frozen_paragraphs,
        sections=sections,
        section_by_paragraph=section_by_paragraph,
        container_coverage=dict(flow.container_policy),
        revision_inventory={},
        excluded_parts=(),
    )
    observation_id = "rm_obs_v1:" + hashlib.sha256(name.encode("utf-8")).hexdigest()
    return ParagraphHistoryObservation(
        observation_id=observation_id,
        snapshot=snapshot,
    )


def _seed(
    observation: ParagraphHistoryObservation,
    paragraph_index: int = 0,
) -> ParagraphHistorySeed:
    paragraph = observation.snapshot.paragraphs[paragraph_index]
    return ParagraphHistorySeed(
        observation_id=observation.observation_id,
        paragraph_ref=_paragraph_ref(observation.snapshot, paragraph),
    )


def _resolution(trace: history.ParagraphHistoryTrace, position: int):
    step = next(item for item in trace.steps if item.position == position)
    assert step.resolution is not None
    return step.resolution


def test_liability_cap_r4_to_r1_produces_three_rejected_projection_links() -> None:
    observations = [
        _observation("r1", [_ParagraphSpec("Liability cap R1")]),
        _observation(
            "r2",
            [_ParagraphSpec("Liability cap R2", rejected="Liability cap R1")],
        ),
        _observation(
            "r3",
            [_ParagraphSpec("Liability cap R3", rejected="Liability cap R2")],
        ),
        _observation(
            "r4",
            [_ParagraphSpec("Liability cap R4", rejected="Liability cap R3")],
        ),
    ]

    trace = resolve_paragraph_history(observations, _seed(observations[-1]))

    assert [step.position for step in trace.steps] == [3, 2, 1, 0]
    assert [
        step.resolution.reason
        for step in trace.steps[1:]
        if step.resolution is not None
    ] == ["rejected_projection_unique"] * 3
    links = [
        step.selected_paragraph.support_to_higher[0]
        for step in trace.steps[1:]
        if step.selected_paragraph is not None
    ]
    assert len(links) == 3
    assert all(link.relationship_type == "rejected_projection_equality" for link in links)
    assert all(link.relationship_id.startswith("ph_rel_v1:") for link in links)
    assert all(link.lower_position + 1 == link.higher_position for link in links)
    assert all(link.basis["full_text_compared"] is True for link in links)


def test_unique_current_plus_unavailable_projection_remains_unresolved() -> None:
    lower = _observation("lower", [_ParagraphSpec("Same wording")])
    higher = _observation(
        "higher",
        [_ParagraphSpec("Same wording", unavailable=True)],
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    step = trace.steps[1]

    assert step.selected_paragraph is None
    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "unresolved",
        "rejected_projection_unavailable",
    )
    assert step.resolution.current_candidate_count == 1
    assert step.resolution.candidate_count == 1
    assert step.candidates[0].evidence_types == ("exact_content_equality",)


def test_two_identical_current_paragraphs_are_ambiguous_even_when_projection_unavailable() -> None:
    lower = _observation(
        "lower",
        [_ParagraphSpec("Duplicate"), _ParagraphSpec("Duplicate")],
    )
    higher = _observation(
        "higher",
        [_ParagraphSpec("Duplicate", unavailable=True)],
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    step = trace.steps[1]

    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "ambiguous",
        "multiple_exact_candidates",
    )
    assert step.resolution.current_candidate_count == 2
    assert step.resolution.candidate_count == 2
    assert len({candidate.paragraph_observation_id for candidate in step.candidates}) == 2


def test_distinct_current_and_rejected_candidates_are_ambiguous() -> None:
    lower = _observation(
        "lower",
        [_ParagraphSpec("New wording"), _ParagraphSpec("Old wording")],
    )
    higher = _observation(
        "higher",
        [_ParagraphSpec("New wording", rejected="Old wording")],
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    step = trace.steps[1]

    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "ambiguous",
        "multiple_exact_candidates",
    )
    assert step.resolution.current_candidate_count == 1
    assert step.resolution.rejected_candidate_count == 1
    assert {candidate.evidence_types for candidate in step.candidates} == {
        ("exact_content_equality",),
        ("rejected_projection_equality",),
    }


def test_duplicate_exclusions_headings_remain_navigation_only() -> None:
    lower = _observation(
        "lower",
        [
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Candidate A"),
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Candidate B"),
        ],
    )
    higher = _observation(
        "higher",
        [
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Selected clause"),
        ],
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher, 1))
    step = trace.steps[1]

    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "unresolved",
        "navigation_only",
    )
    assert step.resolution.candidate_count == 0
    assert len(step.navigation_candidates) == 2
    assert all(
        candidate.evidence_basis["evidence_class"] == "navigation_only"
        for candidate in step.navigation_candidates
    )


def test_post_capture_navigation_mapping_is_immutable_and_stable() -> None:
    lower = _observation(
        "lower",
        [
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Candidate A"),
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Candidate B"),
        ],
    )
    higher = _observation(
        "higher",
        [
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Selected clause"),
        ],
    )
    mutable_source = dict(higher.snapshot.section_by_paragraph)
    higher = replace(
        higher,
        snapshot=replace(
            higher.snapshot,
            section_by_paragraph=mutable_source,
        ),
    )

    before = resolve_paragraph_history([lower, higher], _seed(higher, 1))
    mutable_source.clear()
    with pytest.raises(AttributeError):
        getattr(higher.snapshot.section_by_paragraph, "clear")()
    after = resolve_paragraph_history([lower, higher], _seed(higher, 1))

    assert before == after
    step = after.steps[1]
    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "unresolved",
        "navigation_only",
    )
    assert len(step.navigation_candidates) == 2


def test_malformed_cached_navigation_mapping_fails_before_resolution() -> None:
    lower = _observation("lower", [_ParagraphSpec("Different")])
    higher = _observation(
        "higher",
        [
            _ParagraphSpec("Exclusions", heading=True),
            _ParagraphSpec("Selected clause"),
        ],
    )
    higher = replace(
        higher,
        snapshot=replace(higher.snapshot, section_by_paragraph={}),
    )

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history([lower, higher], _seed(higher, 1))

    assert error.value.code == "snapshot_integrity_error"


@pytest.mark.parametrize(
    ("middle_specs", "latest_text", "expected_middle", "expected_lowest"),
    [
        (
            [_ParagraphSpec("Target"), _ParagraphSpec("Target")],
            "Target",
            ("ambiguous", "multiple_exact_candidates"),
            ("unresolved", "blocked_by_higher_ambiguity"),
        ),
        (
            [_ParagraphSpec("Different")],
            "Target",
            ("unresolved", "no_match_in_declared_scope"),
            ("unresolved", "blocked_by_higher_unresolved"),
        ),
    ],
)
def test_lower_positions_have_the_correct_blocked_reason_and_no_gap_transition(
    middle_specs: list[_ParagraphSpec],
    latest_text: str,
    expected_middle: tuple[str, str],
    expected_lowest: tuple[str, str],
) -> None:
    lowest = _observation("lowest", [_ParagraphSpec("Target")])
    middle = _observation("middle", middle_specs)
    latest = _observation("latest", [_ParagraphSpec(latest_text)])

    trace = resolve_paragraph_history([lowest, middle, latest], _seed(latest))

    middle_step = trace.steps[1]
    lowest_step = trace.steps[2]
    assert middle_step.resolution is not None
    assert lowest_step.resolution is not None
    assert (middle_step.resolution.state, middle_step.resolution.reason) == expected_middle
    assert (lowest_step.resolution.state, lowest_step.resolution.reason) == expected_lowest
    assert lowest_step.selected_paragraph is None
    assert lowest_step.candidates == ()
    assert lowest_step.resolution.higher_rejected_projection_complete is None


def test_blocked_reason_follows_the_immediately_higher_step(monkeypatch) -> None:
    oldest = _observation("oldest", [_ParagraphSpec("Target")])
    lower = _observation("lower", [_ParagraphSpec("Target")])
    ambiguous = _observation(
        "ambiguous",
        [_ParagraphSpec("Target"), _ParagraphSpec("Target")],
    )
    latest = _observation("latest", [_ParagraphSpec("Target")])
    exact_positions: list[int] = []
    exact_candidates = history._exact_candidates

    def tracked_exact_candidates(*args, **kwargs):
        exact_positions.append(args[1])
        return exact_candidates(*args, **kwargs)

    monkeypatch.setattr(history, "_exact_candidates", tracked_exact_candidates)

    trace = resolve_paragraph_history(
        [oldest, lower, ambiguous, latest],
        _seed(latest),
    )

    assert exact_positions == [2]
    assert [
        (step.resolution.state, step.resolution.reason)
        for step in trace.steps[1:]
        if step.resolution is not None
    ] == [
        ("ambiguous", "multiple_exact_candidates"),
        ("unresolved", "blocked_by_higher_ambiguity"),
        ("unresolved", "blocked_by_higher_unresolved"),
    ]
    for step in trace.steps[2:]:
        assert step.selected_paragraph is None
        assert step.candidates == ()
        assert step.resolution is not None
        assert step.resolution.higher_rejected_projection_complete is None


def test_flattened_revision_stops_the_chain_honestly() -> None:
    oldest = _observation("oldest", [_ParagraphSpec("Version A")])
    flattened = _observation("flattened", [_ParagraphSpec("Version B")])
    latest = _observation(
        "latest",
        [_ParagraphSpec("Version C", rejected="Version B")],
    )

    trace = resolve_paragraph_history([oldest, flattened, latest], _seed(latest))

    assert _resolution(trace, 1).reason == "rejected_projection_unique"
    selected = trace.steps[1].selected_paragraph
    assert selected is not None
    assert selected.rejected_pending["equals_current"] is True
    assert selected.rejected_pending["match_eligible"] is False
    assert _resolution(trace, 0).reason == "no_match_in_declared_scope"


def test_hash_prefilter_never_creates_a_current_match_without_full_string_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lower = _observation("lower", [_ParagraphSpec("Different lower text")])
    higher = _observation("higher", [_ParagraphSpec("Selected higher text")])
    monkeypatch.setattr(history, "_hash_prefilter_hit", lambda left, right: True)

    trace = resolve_paragraph_history([lower, higher], _seed(higher))

    assert _resolution(trace, 0).reason == "no_match_in_declared_scope"
    assert trace.steps[1].candidates == ()


def test_hash_prefilter_never_creates_a_rejected_match_without_full_string_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = "Rejected text A"
    lower = _observation("lower", [_ParagraphSpec("Rejected text B")])
    higher = _observation(
        "higher",
        [_ParagraphSpec("Current text", rejected=rejected)],
    )
    monkeypatch.setattr(history, "_hash_prefilter_hit", lambda left, right: True)

    trace = resolve_paragraph_history([lower, higher], _seed(higher))

    assert _resolution(trace, 0).reason == "no_match_in_declared_scope"
    assert trace.steps[1].candidates == ()


def test_byte_identical_observations_keep_distinct_paragraph_observation_identity() -> None:
    snapshot_owner = _observation(
        "captured-once",
        [_ParagraphSpec("Byte-identical paragraph")],
        file_identity="same bytes",
    )
    lower = ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "1" * 64,
        snapshot=snapshot_owner.snapshot,
    )
    higher = ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "2" * 64,
        snapshot=snapshot_owner.snapshot,
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    lower_selected = trace.steps[1].selected_paragraph
    higher_selected = trace.steps[0].selected_paragraph

    assert lower_selected is not None
    assert higher_selected is not None
    assert lower_selected.paragraph_id == higher_selected.paragraph_id
    assert (
        lower_selected.paragraph_observation_id
        != higher_selected.paragraph_observation_id
    )
    relationship = lower_selected.support_to_higher[0]
    assert (
        relationship.lower_paragraph_observation_id
        != relationship.higher_paragraph_observation_id
    )


def test_projection_is_lazy_and_uses_each_selected_paragraphs_own_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowest = _observation(
        "lowest",
        [_ParagraphSpec("Unrelated A"), _ParagraphSpec("Unrelated B")],
    )
    middle = _observation("middle", [_ParagraphSpec("Version B")])
    latest = _observation(
        "latest",
        [_ParagraphSpec("Version C", rejected="Version B")],
    )
    real_builder = history.build_paragraph_projection_v1
    calls: list[str] = []

    def checked_builder(paragraph: etree._Element, body_flow):
        text, _ = _accepted_current_text(paragraph)
        calls.append(text)
        assert any(item.element is paragraph for item in body_flow.paragraphs)
        return real_builder(paragraph, body_flow)

    monkeypatch.setattr(history, "build_paragraph_projection_v1", checked_builder)

    trace = resolve_paragraph_history([lowest, middle, latest], _seed(latest))

    assert calls == ["Version C", "Version B"]
    assert _resolution(trace, 1).reason == "rejected_projection_unique"
    assert _resolution(trace, 0).reason == "no_match_in_declared_scope"


def test_candidate_union_deduplicates_one_paragraph_supported_by_both_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lower = _observation("lower", [_ParagraphSpec("Same")])
    higher = _observation("higher", [_ParagraphSpec("Same")])

    def dual_basis_projection(paragraph: etree._Element, body_flow):
        text, _ = _accepted_current_text(paragraph)
        return {
            "schema_version": "paragraph_projection.v1",
            "mode": "pending_text_revisions_rejected_v1",
            "projection_status": "complete",
            "unavailable_reasons": [],
            "text_state": "nonempty",
            "equals_current": False,
            "has_non_whitespace": True,
            "match_eligible": True,
            "projection_text_sha256": _sha256_text(text),
            "text_length": len(text),
            "text": text,
            "move_wrapper_visibility_applied": False,
            "move_pairing": "not_attempted",
        }

    monkeypatch.setattr(
        history,
        "build_paragraph_projection_v1",
        dual_basis_projection,
    )

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    step = trace.steps[1]

    assert step.resolution is not None
    assert step.resolution.candidate_count == 1
    assert step.resolution.reason == "exact_current_unique"
    assert step.candidates[0].evidence_types == (
        "exact_content_equality",
        "rejected_projection_equality",
    )
    assert len(step.candidates[0].relationships) == 2


def test_snapshot_loader_retains_the_descriptor_captured_body_flow(
    demo_dir,
) -> None:
    path = sorted(demo_dir.glob("*.docx"))[0]
    snapshot = _load_snapshot_from_payload(
        path.read_bytes(),
        path="descriptor-captured-label-only.docx",
    )
    paragraph = next(item for item in snapshot.paragraphs if item.text.strip())

    projection = build_paragraph_projection_v1(
        paragraph.element,
        snapshot.body_flow,
    )

    assert any(item.element is paragraph.element for item in snapshot.body_flow.paragraphs)
    assert projection["schema_version"] == "paragraph_projection.v1"


def test_seed_must_name_the_latest_declared_observation() -> None:
    lower = _observation("lower", [_ParagraphSpec("Lower")])
    higher = _observation("higher", [_ParagraphSpec("Higher")])

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history([lower, higher], _seed(lower))

    assert error.value.code == "seed_not_last_declared_position"


def test_post_capture_xml_mutation_fails_before_a_trace_is_returned() -> None:
    lower = _observation("lower", [_ParagraphSpec("Captured text")])
    higher = _observation("higher", [_ParagraphSpec("Captured text")])
    text_node = next(higher.snapshot.paragraphs[0].element.iter(w("t")))
    text_node.text = "Mutated after capture"

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history([lower, higher], _seed(higher))

    assert error.value.code == "snapshot_integrity_error"
    assert error.value.detail == "captured snapshot integrity check failed"


@pytest.mark.parametrize(
    "corruption",
    [
        {"text": "Malformed cached text"},
        {"text_sha256": None},
    ],
)
def test_malformed_cached_current_facts_fail_before_resolution(
    corruption: dict[str, object],
) -> None:
    lower = _observation("lower", [_ParagraphSpec("Captured text")])
    higher = _observation("higher", [_ParagraphSpec("Captured text")])
    malformed = replace(lower.snapshot.paragraphs[0], **corruption)
    lower = replace(
        lower,
        snapshot=replace(lower.snapshot, paragraphs=(malformed,)),
    )

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history([lower, higher], _seed(higher))

    assert error.value.code == "snapshot_integrity_error"


class _OversizedIndexTrap(Sequence[ParagraphHistoryObservation]):
    def __len__(self) -> int:
        return history.MAX_HISTORY_OBSERVATIONS + 1

    def __getitem__(self, index):
        raise AssertionError(f"unexpected observation access at {index}")


def test_observation_limit_refuses_from_length_before_any_indexing() -> None:
    seed_observation = _observation("seed", [_ParagraphSpec("Seed")])

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history(_OversizedIndexTrap(), _seed(seed_observation))

    assert error.value.code == "resource_limit_exceeded"


def test_exactly_maximum_observations_remain_accepted() -> None:
    owner = _observation("owner", [_ParagraphSpec("Same exact paragraph")])
    observations = [
        ParagraphHistoryObservation(
            observation_id=f"rm_obs_v1:{index:064x}",
            snapshot=owner.snapshot,
        )
        for index in range(history.MAX_HISTORY_OBSERVATIONS)
    ]

    trace = resolve_paragraph_history(observations, _seed(observations[-1]))

    assert len(trace.steps) == history.MAX_HISTORY_OBSERVATIONS
    assert all(
        step.resolution is None or step.resolution.state == "exact_unique"
        for step in trace.steps
    )


@pytest.mark.parametrize("malformed_index", [0.0, False])
def test_malformed_cached_paragraph_index_fails_before_resolution(
    malformed_index: object,
) -> None:
    lower = _observation("lower", [_ParagraphSpec("Same text")])
    higher = _observation("higher", [_ParagraphSpec("Same text")])
    malformed = replace(
        lower.snapshot.paragraphs[0],
        paragraph_index=malformed_index,
    )
    lower = replace(
        lower,
        snapshot=replace(lower.snapshot, paragraphs=(malformed,)),
    )

    with pytest.raises(HistoryResolutionError) as error:
        resolve_paragraph_history([lower, higher], _seed(higher))

    assert error.value.code == "snapshot_integrity_error"
    assert error.value.detail == "captured snapshot integrity check failed"


def test_exact_non_boolean_integer_paragraph_index_remains_accepted() -> None:
    lower = _observation("lower", [_ParagraphSpec("Same text")])
    higher = _observation("higher", [_ParagraphSpec("Same text")])

    trace = resolve_paragraph_history([lower, higher], _seed(higher))
    step = trace.steps[1]

    assert step.resolution is not None
    assert (step.resolution.state, step.resolution.reason) == (
        "exact_unique",
        "exact_current_unique",
    )
    assert step.selected_paragraph is not None
    assert step.selected_paragraph.paragraph_ref["paragraph_index"] == 0
    assert type(step.selected_paragraph.paragraph_ref["paragraph_index"]) is int
