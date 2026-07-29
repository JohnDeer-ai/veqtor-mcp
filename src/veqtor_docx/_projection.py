# SPDX-License-Identifier: Apache-2.0
"""Internal Stage 3C paragraph projection contracts.

This module builds the complete ``paragraph_projection.v1`` object without
adding a public tool or changing the frozen v0.3 MCP contract.  The literal
visibility primitive remains in :mod:`veqtor_docx._ooxml`; this layer owns the
paragraph structural-context scan, availability decision, and closed result.
"""

from __future__ import annotations

import hashlib

from lxml import etree

from ._ooxml import (
    MOVE_REVISION_TAGS,
    TEXT_REVISION_TAGS,
    ArchiveValidationError,
    CanonicalBodyFlow,
    ResourceLimitError,
    current_text_atom,
    iter_canonical_paragraph_nodes,
    pending_text_revisions_rejected_text,
    w,
)

PARAGRAPH_PROJECTION_SCHEMA_VERSION = "paragraph_projection.v1"
PENDING_REJECTED_MODE = "pending_text_revisions_rejected_v1"
MAX_ACCEPTED_CURRENT_CHARS_PER_PARAGRAPH = 50_000
MAX_REJECTED_PROJECTION_CHARS_PER_PARAGRAPH = 50_000

_XSD_WHITESPACE_V1 = frozenset("\t\n\r ")
_UNAVAILABLE_REASONS = (
    "stray_deleted_text",
    "existence_affecting_revision",
    "declared_scope_incomplete",
)
_CELL_EXISTENCE_TAGS = frozenset({w("cellIns"), w("cellDel")})
_STRUCTURAL_PROPERTY_OWNERS = {
    w("trPr"): w("tr"),
    w("tcPr"): w("tc"),
    w("tblPr"): w("tbl"),
}
_PROPERTY_SUBTREE_TAGS = frozenset(
    {
        w("customXmlPr"),
        w("pPr"),
        w("rPr"),
        w("sdtEndPr"),
        w("sdtPr"),
        w("sectPr"),
        w("tblGrid"),
        w("tblPr"),
        w("tblPrEx"),
        w("tcPr"),
        w("trPr"),
    }
)


def _is_descendant_of(element: etree._Element, ancestor: etree._Element) -> bool:
    return element == ancestor or any(
        parent == ancestor for parent in element.iterancestors()
    )


def _require_selected_paragraph(
    paragraph: etree._Element,
    body_flow: CanonicalBodyFlow,
) -> None:
    matches = [
        item for item in body_flow.paragraphs if item.element == paragraph
    ]
    if len(matches) != 1:
        raise ArchiveValidationError(
            "selected paragraph is not uniquely owned by canonical_body_flow_v1"
        )


def _current_text(paragraph: etree._Element) -> str:
    return "".join(
        contribution
        for node in iter_canonical_paragraph_nodes(paragraph)
        if (contribution := current_text_atom(node, boundary=paragraph)) is not None
    )


def _enforce_text_limit(text: str, *, limit: str, allowed_count: int) -> None:
    observed_count = len(text)
    if observed_count > allowed_count:
        raise ResourceLimitError(
            limit,
            "paragraph text exceeds the character limit",
            allowed_count=allowed_count,
            observed_count=observed_count,
        )


def _has_stray_deleted_text(paragraph: etree._Element) -> bool:
    for node in paragraph.iter(w("delText")):
        if _containing_paragraph(node) != paragraph:
            continue
        deletion_like = False
        for ancestor in node.iterancestors():
            if ancestor.tag == w("p"):
                break
            if ancestor.tag in {w("del"), w("moveFrom")}:
                deletion_like = True
        if not deletion_like:
            return True
    return False


def _has_selected_exclusion(
    paragraph: etree._Element,
    body_flow: CanonicalBodyFlow,
) -> bool:
    return any(
        _containing_paragraph(excluded.element) == paragraph
        for excluded in body_flow.excluded_subtrees
    )


def _containing_paragraph(element: etree._Element) -> etree._Element | None:
    if element.tag == w("p"):
        return element
    return next(
        (ancestor for ancestor in element.iterancestors() if ancestor.tag == w("p")),
        None,
    )


def _validate_exclusion_attribution(body_flow: CanonicalBodyFlow) -> None:
    canonical_paragraphs = {item.element for item in body_flow.paragraphs}
    for excluded in body_flow.excluded_subtrees:
        containing_paragraph = _containing_paragraph(excluded.element)
        if containing_paragraph in canonical_paragraphs:
            continue
        raise ArchiveValidationError(
            "excluded text-bearing subtree has no attributable canonical paragraph"
        )


def _validate_stray_deleted_text_attribution(body_flow: CanonicalBodyFlow) -> None:
    if not body_flow.paragraphs:
        return
    canonical_paragraphs = {item.element for item in body_flow.paragraphs}
    document = body_flow.paragraphs[0].element.getroottree().getroot()
    for node in document.iter(w("delText")):
        deletion_like = False
        for ancestor in node.iterancestors():
            if ancestor.tag == w("p"):
                break
            if ancestor.tag in {w("del"), w("moveFrom")}:
                deletion_like = True
        if deletion_like:
            continue
        if _containing_paragraph(node) not in canonical_paragraphs:
            raise ArchiveValidationError(
                "stray deleted text has no attributable canonical paragraph"
            )


def _direct_owner(
    property_element: etree._Element,
    owner_tag: str,
    *,
    revision_name: str,
) -> etree._Element:
    owner = property_element.getparent()
    if owner is None or owner.tag != owner_tag:
        raise ArchiveValidationError(
            f"{revision_name} has no attributable structural owner"
        )
    return owner


def _existence_revision_owner(
    element: etree._Element,
) -> etree._Element | None | object:
    """Return an owning element, ``None`` for text-neutral, or a sentinel.

    The sentinel means that ``element`` is not an existence-affecting
    occurrence.  A recognized occurrence with no closed owner refuses via
    ``ArchiveValidationError`` instead of poisoning an arbitrary paragraph.
    """
    not_existence_affecting = _existence_revision_owner
    local_name = etree.QName(element).localname

    if element.tag in _CELL_EXISTENCE_TAGS:
        parent = element.getparent()
        if parent is None or parent.tag != w("tcPr"):
            raise ArchiveValidationError(
                f"{local_name} has no attributable table-cell owner"
            )
        return _direct_owner(parent, w("tc"), revision_name=local_name)

    if element.tag not in TEXT_REVISION_TAGS:
        return not_existence_affecting
    parent = element.getparent()
    if parent is None:
        return not_existence_affecting

    if parent.tag == w("rPr"):
        paragraph_properties = parent.getparent()
        if paragraph_properties is None or paragraph_properties.tag != w("pPr"):
            return not_existence_affecting
        owner = paragraph_properties.getparent()
        if owner is None or owner.tag != w("p"):
            raise ArchiveValidationError(
                f"paragraphMark{local_name.title()} has no attributable paragraph owner"
            )
        return owner

    if parent.tag in _STRUCTURAL_PROPERTY_OWNERS:
        return _direct_owner(
            parent,
            _STRUCTURAL_PROPERTY_OWNERS[parent.tag],
            revision_name=f"{etree.QName(parent).localname}{local_name.title()}",
        )

    if parent.tag == w("sectPr"):
        properties_owner = parent.getparent()
        if properties_owner is not None and properties_owner.tag == w("body"):
            return None
        if properties_owner is None or properties_owner.tag != w("pPr"):
            raise ArchiveValidationError(
                f"sectPr{local_name.title()} has no attributable section owner"
            )
        owner = properties_owner.getparent()
        if owner is None or owner.tag != w("p"):
            raise ArchiveValidationError(
                f"sectPr{local_name.title()} has no attributable paragraph owner"
            )
        return owner

    return not_existence_affecting


def _has_existence_affecting_revision(
    paragraph: etree._Element,
    body_flow: CanonicalBodyFlow,
) -> bool:
    affected = False
    document = paragraph.getroottree().getroot()
    sentinel = _existence_revision_owner
    for element in document.iter():
        if element.tag not in TEXT_REVISION_TAGS | _CELL_EXISTENCE_TAGS:
            continue
        owner = _existence_revision_owner(element)
        if owner is sentinel or owner is None:
            continue
        if _is_descendant_of(paragraph, owner):
            affected = True
    return affected


def _validate_revision_placements(body_flow: CanonicalBodyFlow) -> None:
    """Refuse wrappers that have neither inline nor structural attribution."""
    if not body_flow.paragraphs:
        return
    document = body_flow.paragraphs[0].element.getroottree().getroot()
    canonical_nodes = {
        node
        for item in body_flow.paragraphs
        for node in iter_canonical_paragraph_nodes(item.element)
    }
    exclusion_roots = {item.element for item in body_flow.excluded_subtrees}
    sentinel = _existence_revision_owner

    for element in document.iter():
        if element.tag not in TEXT_REVISION_TAGS | MOVE_REVISION_TAGS:
            continue
        owner = _existence_revision_owner(element)
        if owner is not sentinel:
            continue
        if element in canonical_nodes:
            continue

        exclusion_kind = body_flow.exclusion_kind_for(element)
        if exclusion_kind is None or element in exclusion_roots:
            raise ArchiveValidationError(
                f"{etree.QName(element).localname} has no valid paragraph placement"
            )
        for ancestor in element.iterancestors():
            if ancestor.tag == w("p"):
                break
            if ancestor.tag in _PROPERTY_SUBTREE_TAGS:
                raise ArchiveValidationError(
                    f"{etree.QName(element).localname} is illegally placed in properties"
                )


def _move_wrapper_present(paragraph: etree._Element) -> bool:
    return any(
        node.tag in MOVE_REVISION_TAGS
        for node in iter_canonical_paragraph_nodes(paragraph)
    )


def build_paragraph_projection_v1(
    paragraph: etree._Element,
    body_flow: CanonicalBodyFlow,
) -> dict[str, object]:
    """Build one complete closed rejected-pending paragraph projection.

    ``paragraph`` must be one exact member of ``body_flow``.  Availability
    reasons are preserved uniquely in their frozen canonical order.  Text is
    never truncated: exactly 50,000 Unicode scalar values pass and 50,001
    refuse before a projection object is returned.
    """
    _require_selected_paragraph(paragraph, body_flow)
    _validate_exclusion_attribution(body_flow)
    _validate_stray_deleted_text_attribution(body_flow)
    _validate_revision_placements(body_flow)

    current = _current_text(paragraph)
    _enforce_text_limit(
        current,
        limit="accepted_current_chars_per_paragraph",
        allowed_count=MAX_ACCEPTED_CURRENT_CHARS_PER_PARAGRAPH,
    )
    rejected = pending_text_revisions_rejected_text(paragraph)
    _enforce_text_limit(
        rejected,
        limit="rejected_projection_chars_per_paragraph",
        allowed_count=MAX_REJECTED_PROJECTION_CHARS_PER_PARAGRAPH,
    )

    unavailable = {
        "stray_deleted_text": _has_stray_deleted_text(paragraph),
        "existence_affecting_revision": _has_existence_affecting_revision(
            paragraph,
            body_flow,
        ),
        "declared_scope_incomplete": _has_selected_exclusion(paragraph, body_flow),
    }
    reasons = [reason for reason in _UNAVAILABLE_REASONS if unavailable[reason]]
    base: dict[str, object] = {
        "schema_version": PARAGRAPH_PROJECTION_SCHEMA_VERSION,
        "mode": PENDING_REJECTED_MODE,
        "projection_status": "unavailable" if reasons else "complete",
        "unavailable_reasons": reasons,
    }
    if reasons:
        return {
            **base,
            "text_state": None,
            "equals_current": None,
            "has_non_whitespace": False,
            "match_eligible": False,
            "projection_text_sha256": None,
            "text_length": None,
            "text": None,
            "move_wrapper_visibility_applied": False,
            "move_pairing": "not_attempted",
        }

    has_non_whitespace = any(
        character not in _XSD_WHITESPACE_V1 for character in rejected
    )
    equals_current = rejected == current
    text_state = "nonempty" if rejected else "empty"
    return {
        **base,
        "text_state": text_state,
        "equals_current": equals_current,
        "has_non_whitespace": has_non_whitespace,
        "match_eligible": (
            text_state == "nonempty" and has_non_whitespace and not equals_current
        ),
        "projection_text_sha256": hashlib.sha256(
            rejected.encode("utf-8")
        ).hexdigest(),
        "text_length": len(rejected),
        "text": rejected,
        "move_wrapper_visibility_applied": _move_wrapper_present(paragraph),
        "move_pairing": "not_attempted",
    }
