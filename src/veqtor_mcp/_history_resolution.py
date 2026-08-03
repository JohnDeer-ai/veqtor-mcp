# SPDX-License-Identifier: Apache-2.0
"""Internal exact adjacent paragraph-history resolution.

The resolver consumes an already ordered sequence of descriptor-captured
inspection snapshots.  It never resolves paths, opens DOCX files, infers
ordering, or treats headings as paragraph identity.  Exact hashes are only a
candidate prefilter: every relationship is confirmed against the complete
Unicode strings retained by the same immutable snapshot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from veqtor_docx._projection import build_paragraph_projection_v1
from veqtor_docx.inspect import (
    _Paragraph,
    _Section,
    _Snapshot,
    _paragraph_ref,
    _resolve_paragraph,
    _section_ref,
)

from . import records


MAX_HISTORY_OBSERVATIONS = 500
MAX_HISTORY_NAVIGATION_CANDIDATES = 10_000
_DOCUMENT_PART = "word/document.xml"
_XSD_WHITESPACE_V1 = frozenset("\t\n\r ")
_EVIDENCE_ORDER = {
    "exact_content_equality": 0,
    "rejected_projection_equality": 1,
}


class HistoryResolutionError(ValueError):
    """One fail-closed refusal at the internal resolver boundary."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _frozen_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _digest(value: object) -> str:
    return records._stable_digest(value)


def _derived_id(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{_digest(dict(identity))}"


def _has_non_whitespace(value: str) -> bool:
    return any(character not in _XSD_WHITESPACE_V1 for character in value)


@dataclass(frozen=True, slots=True)
class ParagraphHistoryObservation:
    """One caller-ordered observation backed by a captured snapshot."""

    observation_id: str
    snapshot: _Snapshot


@dataclass(frozen=True, slots=True)
class ParagraphHistorySeed:
    """Exact user seed bound to one declared observation."""

    observation_id: str
    paragraph_ref: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "paragraph_ref", _frozen_object(self.paragraph_ref))


@dataclass(frozen=True, slots=True)
class ParagraphHistoryRelationship:
    """One exact adjacent-position relationship and its complete basis."""

    relationship_id: str
    relationship_type: str
    lower_position: int
    higher_position: int
    lower_observation_id: str
    higher_observation_id: str
    lower_paragraph_observation_id: str
    higher_paragraph_observation_id: str
    lower_paragraph_id: str
    higher_paragraph_id: str
    comparison_text_sha256: str
    basis: Mapping[str, Any]
    derivation_recorded: bool = False
    lineage_verified: bool = False
    chronology_verified: bool = False
    authorship_verified: bool = False
    time_verified: bool = False
    semantic_identity: str = "not_claimed"


@dataclass(frozen=True, slots=True)
class ParagraphHistoryCandidate:
    """One deduplicated lower paragraph plus every applicable exact basis."""

    paragraph_observation_id: str
    paragraph_id: str
    paragraph_ref: Mapping[str, Any]
    evidence_types: tuple[str, ...]
    relationships: tuple[ParagraphHistoryRelationship, ...]


@dataclass(frozen=True, slots=True)
class ParagraphHistoryNavigationCandidate:
    """Exact Stage 3B section navigation, never paragraph identity."""

    navigation_candidate_id: str
    observation_id: str
    seed_section_id: str
    candidate_section_id: str
    section_ref: Mapping[str, Any]
    label: str | None
    heading: str | None
    level: int
    label_basis: str | None
    evidence_basis: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ParagraphHistoryResolution:
    """Closed state and reason for one non-seed declared position."""

    state: str
    reason: str
    current_candidate_count: int
    rejected_candidate_count: int
    candidate_count: int
    higher_rejected_projection_complete: bool | None
    propagation_permitted: bool


@dataclass(frozen=True, slots=True)
class ParagraphHistorySelectedParagraph:
    """One seed or consecutively selected exact paragraph."""

    paragraph_observation_id: str
    paragraph_id: str
    paragraph_ref: Mapping[str, Any]
    current_text: str
    current_text_sha256: str
    rejected_pending: Mapping[str, Any]
    support_to_higher: tuple[ParagraphHistoryRelationship, ...]


@dataclass(frozen=True, slots=True)
class ParagraphHistoryStep:
    """One visible declared position in seed-first descending order."""

    observation_id: str
    position: int
    entry_role: str
    selected_paragraph: ParagraphHistorySelectedParagraph | None
    resolution: ParagraphHistoryResolution | None
    candidates: tuple[ParagraphHistoryCandidate, ...]
    navigation_candidates: tuple[ParagraphHistoryNavigationCandidate, ...]


@dataclass(frozen=True, slots=True)
class ParagraphHistoryTrace:
    """Complete internal resolver result; positions are never omitted."""

    steps: tuple[ParagraphHistoryStep, ...]


@dataclass(frozen=True, slots=True)
class _SelectedWork:
    observation: ParagraphHistoryObservation
    position: int
    paragraph: _Paragraph
    selected: ParagraphHistorySelectedParagraph


@dataclass(slots=True)
class _CandidateWork:
    paragraph: _Paragraph
    paragraph_observation_id: str
    paragraph_id: str
    paragraph_ref: dict[str, Any]
    relationships: dict[str, ParagraphHistoryRelationship]


def _paragraph_identities(
    observation: ParagraphHistoryObservation,
    paragraph: _Paragraph,
) -> tuple[str, str, dict[str, Any]]:
    reference = _paragraph_ref(observation.snapshot, paragraph)
    paragraph_id = _derived_id("rm_par_v1", reference)
    paragraph_observation_id = _derived_id(
        "ph_par_obs_v1",
        {
            "schema_version": "paragraph_observation_identity.v1",
            "observation_id": observation.observation_id,
            "paragraph_ref": reference,
        },
    )
    return paragraph_id, paragraph_observation_id, reference


def _relationship(
    *,
    relationship_type: str,
    lower: ParagraphHistoryObservation,
    lower_position: int,
    lower_paragraph: _Paragraph,
    higher: _SelectedWork,
    comparison_text_sha256: str,
    basis: dict[str, Any],
) -> ParagraphHistoryRelationship:
    lower_paragraph_id, lower_paragraph_observation_id, _ = _paragraph_identities(
        lower,
        lower_paragraph,
    )
    higher_selected = higher.selected
    identity = {
        "schema_version": "paragraph_history_relationship_identity.v1",
        "relationship_type": relationship_type,
        "lower_position": lower_position,
        "higher_position": higher.position,
        "lower_paragraph_observation_id": lower_paragraph_observation_id,
        "higher_paragraph_observation_id": (
            higher_selected.paragraph_observation_id
        ),
        "comparison_text_sha256": comparison_text_sha256,
        "basis": basis,
    }
    return ParagraphHistoryRelationship(
        relationship_id=_derived_id("ph_rel_v1", identity),
        relationship_type=relationship_type,
        lower_position=lower_position,
        higher_position=higher.position,
        lower_observation_id=lower.observation_id,
        higher_observation_id=higher.observation.observation_id,
        lower_paragraph_observation_id=lower_paragraph_observation_id,
        higher_paragraph_observation_id=(
            higher_selected.paragraph_observation_id
        ),
        lower_paragraph_id=lower_paragraph_id,
        higher_paragraph_id=higher_selected.paragraph_id,
        comparison_text_sha256=comparison_text_sha256,
        basis=_frozen_object(basis),
    )


def _current_relationship(
    *,
    lower: ParagraphHistoryObservation,
    lower_position: int,
    lower_paragraph: _Paragraph,
    higher: _SelectedWork,
) -> ParagraphHistoryRelationship:
    basis = {
        "schema_version": "exact_content_equality_basis.v1",
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "part_name": _DOCUMENT_PART,
        "comparison": "complete_unicode_scalar_sequence_v1",
        "full_text_compared": True,
        "paragraph_text_sha256": lower_paragraph.text_sha256,
    }
    return _relationship(
        relationship_type="exact_content_equality",
        lower=lower,
        lower_position=lower_position,
        lower_paragraph=lower_paragraph,
        higher=higher,
        comparison_text_sha256=lower_paragraph.text_sha256,
        basis=basis,
    )


def _rejected_relationship(
    *,
    lower: ParagraphHistoryObservation,
    lower_position: int,
    lower_paragraph: _Paragraph,
    higher: _SelectedWork,
) -> ParagraphHistoryRelationship:
    _, lower_paragraph_observation_id, _ = _paragraph_identities(
        lower,
        lower_paragraph,
    )
    higher_selected = higher.selected
    projection_sha256 = higher_selected.rejected_pending[
        "projection_text_sha256"
    ]
    assert isinstance(projection_sha256, str)
    basis = {
        "schema_version": "rejected_projection_equality_basis.v1",
        "evidence_class": "projection_text_equality_only",
        "part_name": _DOCUMENT_PART,
        "container_policy": "canonical_body_flow_v1",
        "lower_observation_id": lower.observation_id,
        "higher_observation_id": higher.observation.observation_id,
        "lower_paragraph_observation_id": lower_paragraph_observation_id,
        "higher_paragraph_observation_id": (
            higher_selected.paragraph_observation_id
        ),
        "higher_position_projection": "pending_text_revisions_rejected_v1",
        "lower_position_projection": "accepted_current_v1",
        "lower_current_text_sha256": lower_paragraph.text_sha256,
        "higher_rejected_projection_text_sha256": projection_sha256,
        "higher_current_text_sha256": higher_selected.current_text_sha256,
        "comparison": "sha256_then_full_unicode_equality_v1",
        "full_text_compared": True,
        "semantic_identity": "not_claimed",
        "direction_semantics": "projection_role_only",
        "derivation_recorded": False,
        "lineage_verified": False,
        "chronology_verified": False,
        "authorship_verified": False,
        "time_verified": False,
    }
    return _relationship(
        relationship_type="rejected_projection_equality",
        lower=lower,
        lower_position=lower_position,
        lower_paragraph=lower_paragraph,
        higher=higher,
        comparison_text_sha256=projection_sha256,
        basis=basis,
    )


def _selected_work(
    observation: ParagraphHistoryObservation,
    position: int,
    paragraph: _Paragraph,
    support_to_higher: tuple[ParagraphHistoryRelationship, ...],
) -> _SelectedWork:
    paragraph_id, paragraph_observation_id, reference = _paragraph_identities(
        observation,
        paragraph,
    )
    projection = build_paragraph_projection_v1(
        paragraph.element,
        observation.snapshot.body_flow,
    )
    selected = ParagraphHistorySelectedParagraph(
        paragraph_observation_id=paragraph_observation_id,
        paragraph_id=paragraph_id,
        paragraph_ref=_frozen_object(reference),
        current_text=paragraph.text,
        current_text_sha256=paragraph.text_sha256,
        rejected_pending=_frozen_object(projection),
        support_to_higher=support_to_higher,
    )
    return _SelectedWork(
        observation=observation,
        position=position,
        paragraph=paragraph,
        selected=selected,
    )


def _navigation_candidates(
    seed_section: _Section | None,
    seed_section_id: str | None,
    observation: ParagraphHistoryObservation,
    remaining_capacity: int,
) -> tuple[ParagraphHistoryNavigationCandidate, ...]:
    if seed_section is None or seed_section_id is None:
        return ()
    candidates: list[ParagraphHistoryNavigationCandidate] = []
    for section in observation.snapshot.sections:
        signals: list[dict[str, str]] = []
        if seed_section.label is not None and section.label == seed_section.label:
            signals.append(
                {
                    "kind": "label_exact_v1",
                    "value_sha256": hashlib.sha256(
                        seed_section.label.encode("utf-8")
                    ).hexdigest(),
                }
            )
        if seed_section.title is not None and section.title == seed_section.title:
            signals.append(
                {
                    "kind": "heading_exact_v1",
                    "value_sha256": hashlib.sha256(
                        seed_section.title.encode("utf-8")
                    ).hexdigest(),
                }
            )
        if not signals:
            continue
        if len(candidates) >= remaining_capacity:
            raise HistoryResolutionError(
                "resource_limit_exceeded",
                "navigation candidate count exceeds its fixed limit",
            )
        reference = _section_ref(observation.snapshot, section)
        candidate_section_id = _derived_id("rm_sec_v1", reference)
        evidence_basis = {
            "schema_version": "navigation_candidate_basis.v1",
            "signals": signals,
            "evidence_class": "navigation_only",
        }
        navigation_id = _derived_id(
            "ph_nav_v1",
            {
                "schema_version": "paragraph_history_navigation_identity.v1",
                "observation_id": observation.observation_id,
                "seed_section_id": seed_section_id,
                "candidate_section_id": candidate_section_id,
                "evidence_basis": evidence_basis,
            },
        )
        candidates.append(
            ParagraphHistoryNavigationCandidate(
                navigation_candidate_id=navigation_id,
                observation_id=observation.observation_id,
                seed_section_id=seed_section_id,
                candidate_section_id=candidate_section_id,
                section_ref=_frozen_object(reference),
                label=section.label,
                heading=section.title,
                level=section.level,
                label_basis=section.label_basis,
                evidence_basis=_frozen_object(evidence_basis),
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.navigation_candidate_id))


def _candidate_work(
    by_id: dict[str, _CandidateWork],
    observation: ParagraphHistoryObservation,
    paragraph: _Paragraph,
) -> _CandidateWork:
    paragraph_id, paragraph_observation_id, reference = _paragraph_identities(
        observation,
        paragraph,
    )
    candidate = by_id.get(paragraph_observation_id)
    if candidate is None:
        candidate = _CandidateWork(
            paragraph=paragraph,
            paragraph_observation_id=paragraph_observation_id,
            paragraph_id=paragraph_id,
            paragraph_ref=reference,
            relationships={},
        )
        by_id[paragraph_observation_id] = candidate
    return candidate


def _exact_candidates(
    lower: ParagraphHistoryObservation,
    lower_position: int,
    higher: _SelectedWork,
) -> tuple[tuple[ParagraphHistoryCandidate, ...], dict[str, _CandidateWork], int, int]:
    projection = higher.selected.rejected_pending
    projection_status = projection.get("projection_status")
    if projection_status not in {"complete", "unavailable"}:
        raise HistoryResolutionError(
            "projection_contract_error",
            "selected paragraph projection has an invalid status",
        )
    rejected_eligible = (
        projection_status == "complete" and projection.get("match_eligible") is True
    )
    rejected_sha256 = projection.get("projection_text_sha256")
    rejected_text = projection.get("text")
    if rejected_eligible and not (
        isinstance(rejected_sha256, str) and isinstance(rejected_text, str)
    ):
        raise HistoryResolutionError(
            "projection_contract_error",
            "eligible rejected projection lacks complete text identity",
        )

    by_id: dict[str, _CandidateWork] = {}
    current_ids: set[str] = set()
    rejected_ids: set[str] = set()
    for paragraph in lower.snapshot.paragraphs:
        if not _has_non_whitespace(paragraph.text):
            continue
        if (
            paragraph.text_sha256 == higher.selected.current_text_sha256
            and paragraph.text == higher.selected.current_text
        ):
            candidate = _candidate_work(by_id, lower, paragraph)
            candidate.relationships["exact_content_equality"] = (
                _current_relationship(
                    lower=lower,
                    lower_position=lower_position,
                    lower_paragraph=paragraph,
                    higher=higher,
                )
            )
            current_ids.add(candidate.paragraph_observation_id)
        if (
            rejected_eligible
            and paragraph.text_sha256 == rejected_sha256
            and paragraph.text == rejected_text
        ):
            candidate = _candidate_work(by_id, lower, paragraph)
            candidate.relationships["rejected_projection_equality"] = (
                _rejected_relationship(
                    lower=lower,
                    lower_position=lower_position,
                    lower_paragraph=paragraph,
                    higher=higher,
                )
            )
            rejected_ids.add(candidate.paragraph_observation_id)

    candidates = []
    for candidate in sorted(by_id.values(), key=lambda item: item.paragraph_observation_id):
        evidence_types = tuple(
            sorted(candidate.relationships, key=_EVIDENCE_ORDER.__getitem__)
        )
        relationships = tuple(candidate.relationships[item] for item in evidence_types)
        candidates.append(
            ParagraphHistoryCandidate(
                paragraph_observation_id=candidate.paragraph_observation_id,
                paragraph_id=candidate.paragraph_id,
                paragraph_ref=_frozen_object(candidate.paragraph_ref),
                evidence_types=evidence_types,
                relationships=relationships,
            )
        )
    return tuple(candidates), by_id, len(current_ids), len(rejected_ids)


def _blocked_resolution(higher_state: str) -> ParagraphHistoryResolution:
    reason = (
        "blocked_by_higher_ambiguity"
        if higher_state == "ambiguous"
        else "blocked_by_higher_unresolved"
    )
    return ParagraphHistoryResolution(
        state="unresolved",
        reason=reason,
        current_candidate_count=0,
        rejected_candidate_count=0,
        candidate_count=0,
        higher_rejected_projection_complete=None,
        propagation_permitted=False,
    )


def resolve_paragraph_history(
    observations: Sequence[ParagraphHistoryObservation],
    seed: ParagraphHistorySeed,
) -> ParagraphHistoryTrace:
    """Resolve one exact seed backward through adjacent declared positions.

    Input ordering is the sole source of position.  The returned sequence is
    seed-first and then descending position, including every blocked lower
    observation after the first ambiguous or unresolved step.
    """
    if not isinstance(seed, ParagraphHistorySeed):
        raise HistoryResolutionError(
            "invalid_seed", "seed must use the immutable internal carrier"
        )
    ordered = tuple(observations)
    if not ordered:
        raise HistoryResolutionError(
            "invalid_observation_sequence", "at least one observation is required"
        )
    if len(ordered) > MAX_HISTORY_OBSERVATIONS:
        raise HistoryResolutionError(
            "resource_limit_exceeded",
            "observation count exceeds its fixed limit",
        )
    if any(
        not isinstance(item, ParagraphHistoryObservation)
        or not isinstance(item.snapshot, _Snapshot)
        for item in ordered
    ):
        raise HistoryResolutionError(
            "invalid_observation_sequence",
            "every observation must use the immutable internal carrier",
        )
    observation_ids = tuple(item.observation_id for item in ordered)
    if any(not isinstance(value, str) or not value for value in observation_ids) or len(
        set(observation_ids)
    ) != len(observation_ids):
        raise HistoryResolutionError(
            "invalid_observation_sequence",
            "observation identifiers must be non-empty and unique",
        )

    latest = ordered[-1]
    if seed.observation_id != latest.observation_id:
        raise HistoryResolutionError(
            "seed_not_last_declared_position",
            "the exact seed must name the latest declared observation",
        )
    seed_paragraph = _resolve_paragraph(latest.snapshot, dict(seed.paragraph_ref))
    if not _has_non_whitespace(seed_paragraph.text):
        raise HistoryResolutionError(
            "seed_not_match_eligible",
            "the selected seed paragraph must contain non-whitespace text",
        )

    latest_position = len(ordered) - 1
    selected = _selected_work(latest, latest_position, seed_paragraph, ())
    seed_section = latest.snapshot.section_by_paragraph.get(
        seed_paragraph.paragraph_index
    )
    seed_section_id = None
    if seed_section is not None:
        seed_section_id = _derived_id(
            "rm_sec_v1", _section_ref(latest.snapshot, seed_section)
        )
    steps = [
        ParagraphHistoryStep(
            observation_id=latest.observation_id,
            position=latest_position,
            entry_role="seed",
            selected_paragraph=selected.selected,
            resolution=None,
            candidates=(),
            navigation_candidates=(),
        )
    ]

    blocked_by: str | None = None
    navigation_candidate_count = 0
    for lower_position in range(latest_position - 1, -1, -1):
        lower = ordered[lower_position]
        navigation = _navigation_candidates(
            seed_section,
            seed_section_id,
            lower,
            MAX_HISTORY_NAVIGATION_CANDIDATES - navigation_candidate_count,
        )
        navigation_candidate_count += len(navigation)
        if blocked_by is not None:
            steps.append(
                ParagraphHistoryStep(
                    observation_id=lower.observation_id,
                    position=lower_position,
                    entry_role="trace_step",
                    selected_paragraph=None,
                    resolution=_blocked_resolution(blocked_by),
                    candidates=(),
                    navigation_candidates=navigation,
                )
            )
            continue

        candidates, candidate_work, current_count, rejected_count = _exact_candidates(
            lower,
            lower_position,
            selected,
        )
        projection_complete = (
            selected.selected.rejected_pending["projection_status"] == "complete"
        )
        candidate_count = len(candidates)
        if candidate_count >= 2:
            state, reason = "ambiguous", "multiple_exact_candidates"
        elif not projection_complete:
            state, reason = "unresolved", "rejected_projection_unavailable"
        elif candidate_count == 1:
            state = "exact_unique"
            reason = (
                "exact_current_unique"
                if current_count == 1
                else "rejected_projection_unique"
            )
        elif navigation:
            state, reason = "unresolved", "navigation_only"
        else:
            state, reason = "unresolved", "no_match_in_declared_scope"

        resolution = ParagraphHistoryResolution(
            state=state,
            reason=reason,
            current_candidate_count=current_count,
            rejected_candidate_count=rejected_count,
            candidate_count=candidate_count,
            higher_rejected_projection_complete=projection_complete,
            propagation_permitted=state == "exact_unique",
        )
        next_selected = None
        if state == "exact_unique":
            candidate = candidates[0]
            work = candidate_work[candidate.paragraph_observation_id]
            next_selected = _selected_work(
                lower,
                lower_position,
                work.paragraph,
                candidate.relationships,
            )
        steps.append(
            ParagraphHistoryStep(
                observation_id=lower.observation_id,
                position=lower_position,
                entry_role="trace_step",
                selected_paragraph=(
                    next_selected.selected if next_selected is not None else None
                ),
                resolution=resolution,
                candidates=candidates,
                navigation_candidates=navigation,
            )
        )
        if next_selected is None:
            blocked_by = state
        else:
            selected = next_selected

    return ParagraphHistoryTrace(steps=tuple(steps))
