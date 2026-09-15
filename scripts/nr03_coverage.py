# SPDX-License-Identifier: Apache-2.0
"""Prospective brief coverage: direct paragraphs or complete public section chains.

Local inspection substantiates original native pages; it never replaces them.
Frozen NR-01 write predicates remain untouched.
"""
from veqtor_docx import inspect_document
from veqtor_docx.inspect import InspectError
from check_codex_acceptance import _digest, _require

COVERAGE_POLICY = "nr03-brief-coverage.v3"
ROLES = {2: "confidentiality", 4: "payment concession", 5: "liability cap",
         6: "complete guarantee condition", 7: "pending exclusivity",
         8: "inventory correspondence", 9: "distinct data-security audit"}


def section_chains(calls, path, *, upper):
    pages = [c for c in calls if not c["failed"] and c["tool"] == "inspect_document"
             and c["arguments"].get("path") == path and c["arguments"].get("mode") == "read"
             and isinstance(c["arguments"].get("selection"), dict)
             and set(c["arguments"]["selection"]) == {"section_ref"} and c["completed_at"] < upper]
    complete = []
    for first in pages:
        if first["arguments"].get("cursor") is not None:
            continue
        selection = first["arguments"]["selection"]
        # Require actual prior native discovery of this section reference.
        discovered = any(not c["failed"] and c["tool"] == "inspect_document"
            and c["arguments"].get("path") == path and c["completed_at"] < first["started_at"]
            and any(row.get("section_ref") == selection["section_ref"] for row in c["payload"].get("sections", []))
            for c in calls)
        if not discovered:
            continue
        chain, current = [], first
        while current is not None:
            try:
                expected = inspect_document(**current["arguments"])
            except (InspectError, TypeError, ValueError):
                break
            # Covers membership, complete text/refs, policies, counts, offsets,
            # cursor binding, limits and actual snapshot. No terminal-flag shortcut.
            if expected.get("selection_kind") != "section" or any(
                    current["payload"].get(k) != v for k, v in expected.items()):
                break
            chain.append(current)
            cursor = expected["next_cursor"]
            if cursor is None:
                complete.append(chain)
                break
            candidates = [c for c in pages if c["started_at"] > current["completed_at"]
                          and c["arguments"].get("selection") == selection
                          and c["arguments"].get("cursor") == cursor]
            current = min(candidates, key=lambda c: c["started_at"]) if candidates else None
    return complete


def brief_coverage(calls, documents, *, upper, delivered_ids=None):
    from check_next_round_acceptance import exact_read_quote
    rows = []
    for path, texts in documents:
        chains = section_chains(calls, path, upper=upper)
        for index, role in ROLES.items():
            evidence = exact_read_quote(calls, path, index, texts[index], upper=upper, return_evidence=True)
            form = "paragraph"
            if not evidence:
                for chain in chains:
                    for page in chain:
                        if any(row["paragraph_ref"]["paragraph_index"] == index for row in page["payload"]["paragraphs"]):
                            evidence = exact_read_quote(calls, path, index, texts[index], upper=upper,
                                                        section_page=page, return_evidence=True)
                            if evidence:
                                evidence = list(dict.fromkeys([c["id"] for c in chain] + evidence))
                                form = "section"
                                break
                    if evidence:
                        break
            _require(evidence, f"selected-issue brief lacks full verified evidence: {path} paragraph {index} ({role})")
            if delivered_ids is not None:
                _require(set(evidence) <= set(delivered_ids),
                         f"brief model delivery incomplete: {path} paragraph {index}")
            rows.append(dict(path=path, paragraph_index=index, role=role, form=form, call_ids=evidence))
    return dict(policy=COVERAGE_POLICY, obligations=rows, raw_coverage_passed=True,
                model_delivery="PASS" if delivered_ids is not None else "OPEN",
                evidence_sha256=_digest(rows))
