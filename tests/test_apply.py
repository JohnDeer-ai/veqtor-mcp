# SPDX-License-Identifier: Apache-2.0
"""apply_edits: fail-closed application of tracked edits with round-trip proof."""

import hashlib
import importlib
import zipfile
from pathlib import Path

import pytest

from veqtor_docx import ApplyError, apply_edits, extract_redlines
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.apply import DEFAULT_AUTHOR, _collateral_outside

TAIL_OLD = " in respect of all claims in aggregate."
TAIL_NEW = " in respect of all claims arising in any Contract Year."


@pytest.fixture
def round2(demo_dir: Path) -> Path:
    return demo_dir / "round-2-counterparty-redline.docx"


def _cap_anchor(path: Path) -> dict:
    result = extract_redlines(str(path))
    unit = next(
        u
        for u in result["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
    )
    return {"change_unit_id": unit["change_unit_id"], "file_sha256": result["file_sha256"]}


def _edit(anchor: dict, delete_text: str = TAIL_OLD, insert_text: str = TAIL_NEW) -> dict:
    return {"anchor": anchor, "delete_text": delete_text, "insert_text": insert_text}


def test_replace_at_anchor_with_round_trip(round2: Path, tmp_path: Path) -> None:
    source_bytes = round2.read_bytes()
    out = tmp_path / "counter.docx"
    result = apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])

    assert result["status"] == "ok"
    assert result["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert result["round_trip_check"]["status"] == "passed"
    assert result["round_trip_check"]["collateral_changes"] == []
    assert round2.read_bytes() == source_bytes  # source untouched

    before = extract_redlines(str(round2))
    after = extract_redlines(str(out))
    strip = lambda u: (u["change_type"], u["author"], u["old_text"], u["new_text"])
    assert [strip(u) for u in before["change_units"]] == [
        strip(u) for u in after["change_units"] if u["author"] != DEFAULT_AUTHOR
    ]
    mine = [u for u in after["change_units"] if u["author"] == DEFAULT_AUTHOR]
    assert len(mine) == 1
    assert mine[0]["change_type"] == "replace"
    assert mine[0]["old_text"] == TAIL_OLD
    assert mine[0]["new_text"] == TAIL_NEW
    assert mine[0]["clause_anchor"] == {"label": "14.2", "heading": "Limitation of Liability"}
    assert mine[0]["date"] is None  # no fabricated timestamps
    assert [str(i) for i in result["applied"][0]["tracked_revision_ids"]] == list(
        mine[0]["reference"]["revision_ids"]
    )


def test_pure_deletion(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "delete-only.docx"
    result = apply_edits(
        str(round2), str(out), [_edit(_cap_anchor(round2), insert_text="")]
    )
    assert result["applied"][0]["inserted_text"] is None
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert mine[0]["change_type"] == "delete"
    assert mine[0]["new_text"] is None


def test_apply_is_deterministic(round2: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    apply_edits(str(round2), str(a), [_edit(_cap_anchor(round2))])
    apply_edits(str(round2), str(b), [_edit(_cap_anchor(round2))])
    assert a.read_bytes() == b.read_bytes()


def test_two_edits_in_distinct_paragraphs(round2: Path, tmp_path: Path) -> None:
    result = extract_redlines(str(round2))
    sha = result["file_sha256"]
    cap = _cap_anchor(round2)
    audit = next(
        u
        for u in result["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "7"
    )
    out = tmp_path / "two.docx"
    outcome = apply_edits(
        str(round2),
        str(out),
        [
            _edit(cap),
            {
                "anchor": {"change_unit_id": audit["change_unit_id"], "file_sha256": sha},
                "delete_text": "ten (10) Business Days' notice",
                "insert_text": "five (5) Business Days' notice",
            },
        ],
    )
    assert len(outcome["applied"]) == 2
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert len(mine) == 2


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda e, sha: {**e, "anchor": {**e["anchor"], "file_sha256": "0" * 64}}, "file_sha256_mismatch"),
        (lambda e, sha: {**e, "anchor": {**e["anchor"], "change_unit_id": "cu_999"}}, "anchor_not_found"),
        (lambda e, sha: {**e, "delete_text": "text that does not exist anywhere"}, "delete_text_not_found"),
        (lambda e, sha: {**e, "delete_text": "aggregate"}, "delete_text_ambiguous"),
        # A span mixing plain text and their pending insertion is unsupported;
        # a span fully inside their insertion is a legitimate counter edit.
        (lambda e, sha: {**e, "delete_text": "exceed USD 50,000"}, "overlaps_tracked_changes"),
        (lambda e, sha: {**e, "delete_text": ""}, "delete_text_missing"),
    ],
)
def test_fail_closed_writes_nothing(round2: Path, tmp_path: Path, mutate, code) -> None:
    edit = _edit(_cap_anchor(round2))
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), [mutate(edit, None)])
    assert err.value.code == code
    assert not out.exists()
    assert not list(tmp_path.iterdir())  # no temp artifacts either


def test_atomicity_one_bad_edit_writes_nothing(round2: Path, tmp_path: Path) -> None:
    good = _edit(_cap_anchor(round2))
    bad = {**good, "delete_text": "text that does not exist anywhere"}
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError):
        apply_edits(str(round2), str(out), [good, bad])
    assert not list(tmp_path.iterdir())


def test_two_disjoint_edits_in_same_paragraph(round2: Path, tmp_path: Path) -> None:
    """Same-paragraph edits apply right-to-left; disjoint spans succeed."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "two-in-one.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [
            _edit(anchor),
            _edit(anchor, delete_text="Except as set out", insert_text="Save as provided"),
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert [(u["old_text"], u["new_text"]) for u in mine] == [
        ("Except as set out", "Save as provided"),
        (TAIL_OLD, TAIL_NEW),
    ]


def test_overlapping_edits_same_paragraph_rejected(round2: Path, tmp_path: Path) -> None:
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="claims in aggregate", insert_text="X"),
                _edit(anchor, delete_text="in aggregate.", insert_text="Y"),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not out.exists()


def test_refuses_to_overwrite_output(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "existing.docx"
    out.write_bytes(b"do not clobber me")
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    assert err.value.code == "output_exists"
    assert out.read_bytes() == b"do not clobber me"


def test_counter_and_reinstate_in_one_call(demo_dir: Path, tmp_path: Path) -> None:
    """The M2 demo sentence: 'restore the 150% cap and reinstate the willful
    misconduct carve-out' — one call against the counterparty's round 4."""
    from veqtor_docx.synthetic import CAP_R3, CAP_R4, CARVEOUT_DROPPED

    r4 = demo_dir / "round-4-counterparty-reply.docx"
    result = extract_redlines(str(r4))
    cap = next(u for u in result["change_units"] if u["change_type"] == "replace")
    anchor = {"change_unit_id": cap["change_unit_id"], "file_sha256": result["file_sha256"]}

    out = tmp_path / "round-5-our-counter.docx"
    outcome = apply_edits(
        str(r4),
        str(out),
        [
            {"anchor": anchor, "delete_text": CAP_R4, "insert_text": CAP_R3},
            {"anchor": anchor, "reinstate_text": CARVEOUT_DROPPED},
        ],
    )
    assert outcome["round_trip_check"]["status"] == "passed"
    assert [a["operation"] for a in outcome["applied"]] == ["counter", "reinstate"]

    after = extract_redlines(str(out))
    counter = next(u for u in after["change_units"] if u["change_type"] == "counter")
    assert counter["author"] == DEFAULT_AUTHOR
    assert (counter["old_text"], counter["new_text"]) == (CAP_R4, CAP_R3)

    # Their proposal stays visible, marked as countered by our strike.
    theirs = next(u for u in after["change_units"] if u["change_type"] == "replace")
    assert theirs["new_text"] == CAP_R4
    assert theirs["countered_by"] == counter["reference"]["revision_ids"][:1]

    reinstated = next(u for u in after["change_units"] if u["change_type"] == "insert")
    assert reinstated["new_text"] == CARVEOUT_DROPPED
    # Their deletion is untouched and still reported.
    assert any(
        u["change_type"] == "delete" and u["old_text"] == CARVEOUT_DROPPED
        for u in after["change_units"]
    )

    # Word idiom: our strike is nested inside their pending insertion.
    document = parse_xml(zipfile.ZipFile(out).read("word/document.xml"))
    nested = [
        (d.get(w("author")), d.getparent().get(w("author")))
        for d in document.iter(w("del"))
        if d.getparent().tag == w("ins")
    ]
    assert nested == [(DEFAULT_AUTHOR, "53")]


def test_counter_pure_strike_without_replacement(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "strike.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [_edit(_cap_anchor(round2), delete_text="USD 50,000", insert_text="")],
    )
    assert result["applied"][0]["operation"] == "counter"
    counter = next(
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["change_type"] == "counter"
    )
    assert counter["old_text"] == "USD 50,000"
    assert counter["new_text"] is None


def test_counter_partial_strike_splits_their_runs(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "partial.docx"
    apply_edits(
        str(round2),
        str(out),
        [_edit(_cap_anchor(round2), delete_text="50,000", insert_text="250,000")],
    )
    after = extract_redlines(str(out))
    counter = next(u for u in after["change_units"] if u["change_type"] == "counter")
    assert (counter["old_text"], counter["new_text"]) == ("50,000", "250,000")
    # The untouched remainder of their proposal is still their unit's text.
    theirs = next(u for u in after["change_units"] if u.get("countered_by"))
    assert theirs["new_text"] == "USD 50,000"


def test_two_counters_in_one_insertion_refused(round2: Path, tmp_path: Path) -> None:
    """Two disjoint counters inside the same pending insertion cannot be laid
    out unambiguously (both replacements would pile up after the host)."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="USD", insert_text="EUR"),
                _edit(anchor, delete_text="50,000", insert_text="250,000"),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not list(tmp_path.iterdir())


def test_counter_with_adjacent_plain_edit_refused(round2: Path, tmp_path: Path) -> None:
    """A plain edit starting immediately after the countered insertion would
    merge with the counter's markup in extraction; refuse, do not confuse."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="USD 50,000", insert_text="USD 250,000"),
                _edit(anchor, delete_text=TAIL_OLD, insert_text=" per claim."),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not list(tmp_path.iterdir())


def test_counter_plus_distant_edit_in_same_paragraph(round2: Path, tmp_path: Path) -> None:
    """With untouched text between the operations, a counter and a plain edit
    coexist in one paragraph and extract as two distinct units."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "combo.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [
            _edit(anchor, delete_text="USD 50,000", insert_text="EUR 60,000"),
            _edit(anchor, delete_text="Except as set out", insert_text="Save as provided"),
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert [(u["change_type"], u["old_text"], u["new_text"]) for u in mine] == [
        ("replace", "Except as set out", "Save as provided"),
        ("counter", "USD 50,000", "EUR 60,000"),
    ]


def _countered_step1(round2: Path, tmp_path: Path) -> tuple[str, dict]:
    """First call: counter 'USD' -> 'EUR'; returns output path and an anchor."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text="USD", insert_text="EUR")],
    )
    result = extract_redlines(str(step1))
    counter = next(u for u in result["change_units"] if u["change_type"] == "counter")
    return str(step1), {
        "change_unit_id": counter["change_unit_id"],
        "file_sha256": result["file_sha256"],
    }


def test_followup_counter_on_countered_insertion_refused(
    round2: Path, tmp_path: Path
) -> None:
    """A second counter on the same insertion cannot be laid out — in the
    same call or any later one. Both variants must be clean planning-time
    refusals, not late round_trip_failed."""
    step1, anchor = _countered_step1(round2, tmp_path)
    for edit in (
        {"anchor": anchor, "delete_text": "50,000", "insert_text": "250,000"},
        {"anchor": anchor, "delete_text": "50,000"},
    ):
        with pytest.raises(ApplyError) as err:
            apply_edits(step1, str(tmp_path / "never.docx"), [edit])
        assert err.value.code == "already_countered"
        assert not (tmp_path / "never.docx").exists()


def test_followup_plain_edit_flush_against_our_replacement_refused(
    round2: Path, tmp_path: Path
) -> None:
    step1, anchor = _countered_step1(round2, tmp_path)
    with pytest.raises(ApplyError) as err:
        apply_edits(
            step1,
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": anchor,
                    "delete_text": TAIL_OLD,
                    "insert_text": " per claim.",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_plain_edit_after_pure_strike_host_refused(round2: Path, tmp_path: Path) -> None:
    """A pure-strike counter leaves no replacement after the host, but the
    strike still surfaces at the host's right edge in extraction — a plain
    edit flush after that host must be refused cleanly, not fail late."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text="USD ", insert_text="")],
    )
    result = extract_redlines(str(step1))
    counter = next(u for u in result["change_units"] if u["change_type"] == "counter")
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(step1),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": counter["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": TAIL_OLD,
                    "insert_text": " per claim.",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_pure_strike_on_host_followed_by_our_markup_refused(
    round2: Path, tmp_path: Path
) -> None:
    """The symmetric order: plain edit first, then a pure-strike counter on
    the host our earlier markup now touches."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text=TAIL_OLD, insert_text=" per claim.")],
    )
    result = extract_redlines(str(step1))
    theirs = next(
        u
        for u in result["change_units"]
        if u["author"] != DEFAULT_AUTHOR
        and u["clause_anchor"]
        and u["clause_anchor"]["label"] == "14.2"
    )
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(step1),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": theirs["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": "USD 50,000",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_round_trip_verdict_classification() -> None:
    """The systematic net: losing a baseline unit is adjacency by definition;
    a missing proposed edit is a genuine round-trip failure."""
    from veqtor_docx.apply import _round_trip_verdict

    baseline = [("replace", "53", None, "a", "b", ("1", "2"))]
    proposed = [("insert", DEFAULT_AUTHOR, None, None, "x", ("9",))]

    assert _round_trip_verdict(baseline, baseline + proposed, proposed) is None

    merged = [("counter", DEFAULT_AUTHOR, None, "ab", "x", ("1", "9"))]
    verdict = _round_trip_verdict(baseline, merged, proposed)
    assert verdict is not None and verdict.code == "adjacent_to_own_revision"

    verdict = _round_trip_verdict(baseline, list(baseline), proposed)
    assert verdict is not None and verdict.code == "round_trip_failed"


def test_followup_edit_with_separating_text_works(round2: Path, tmp_path: Path) -> None:
    """Sequential calls stay a valid recipe when untouched text separates the
    new edit from earlier markup."""
    step1, anchor = _countered_step1(round2, tmp_path)
    out = tmp_path / "step2.docx"
    result = apply_edits(
        str(step1),
        str(out),
        [
            {
                "anchor": anchor,
                "delete_text": "Except as set out",
                "insert_text": "Save as provided",
            }
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    units = extract_redlines(str(out))["change_units"]
    assert [
        (u["change_type"], u["old_text"], u["new_text"])
        for u in units
        if u["author"] == DEFAULT_AUTHOR
    ] == [
        ("replace", "Except as set out", "Save as provided"),
        ("counter", "USD", "EUR"),
    ]


def test_counter_own_insertion_refused(round2: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.docx"
    apply_edits(str(round2), str(first), [_edit(_cap_anchor(round2))])
    result = extract_redlines(str(first))
    mine = next(u for u in result["change_units"] if u["author"] == DEFAULT_AUTHOR)
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(first),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": mine["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": "Contract Year",
                    "insert_text": "Fiscal Year",
                }
            ],
        )
    assert err.value.code == "overlaps_tracked_changes"


def test_reinstate_text_not_found(demo_dir: Path, tmp_path: Path) -> None:
    r4 = demo_dir / "round-4-counterparty-reply.docx"
    result = extract_redlines(str(r4))
    cap = next(u for u in result["change_units"] if u["change_type"] == "replace")
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(r4),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": cap["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "reinstate_text": "text nobody ever deleted",
                }
            ],
        )
    assert err.value.code == "reinstate_text_not_found"
    assert not list(tmp_path.iterdir())


def test_apply_at_table_cell_anchor(round2: Path, tmp_path: Path) -> None:
    """An edit anchored inside a table cell round-trips like any other."""
    result = extract_redlines(str(round2))
    cell_unit = next(
        u
        for u in result["change_units"]
        if u["change_type"] == "replace"
        and u["clause_anchor"]
        and u["clause_anchor"]["label"] == "3.3"
    )
    out = tmp_path / "table-edit.docx"
    outcome = apply_edits(
        str(round2),
        str(out),
        [
            {
                "anchor": {
                    "change_unit_id": cell_unit["change_unit_id"],
                    "file_sha256": result["file_sha256"],
                },
                "delete_text": "%",
                "insert_text": " percent",
            }
        ],
    )
    assert outcome["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert len(mine) == 1
    assert (mine[0]["old_text"], mine[0]["new_text"]) == ("%", " percent")
    assert mine[0]["clause_anchor"]["label"] == "3.3"


def _document_roots(path: Path) -> tuple:
    payload = zipfile.ZipFile(path).read("word/document.xml")
    return parse_xml(payload), parse_xml(payload)


def _fee_cell_paragraph_index(root) -> int:
    paragraphs = list(root.iter(w("p")))
    return next(
        i
        for i, p in enumerate(paragraphs)
        if any(el.tag == w("delText") and el.text == "50" for el in p.iter())
    )


def test_collateral_proof_catches_changes_in_other_table_rows(round2: Path) -> None:
    """The no-collateral proof must be paragraph-granular: a change smuggled
    into ANOTHER row of the table containing the touched paragraph is
    collateral, not exempt."""
    original, mutated = _document_roots(round2)
    touched_index = _fee_cell_paragraph_index(mutated)
    sneak = next(t for t in mutated.iter(w("t")) if t.text == "75%")
    sneak.text = "76%"
    issues = _collateral_outside(original, mutated, {touched_index})
    assert issues, "a change in a sibling table row must be reported as collateral"


def test_collateral_proof_accepts_changes_only_in_touched_paragraph(round2: Path) -> None:
    original, mutated = _document_roots(round2)
    touched_index = _fee_cell_paragraph_index(mutated)
    paragraphs = list(mutated.iter(w("p")))
    target = next(t for t in paragraphs[touched_index].iter(w("t")) if t.text == "%")
    target.text = "%%"
    assert _collateral_outside(original, mutated, {touched_index}) == []


def test_collateral_proof_catches_structural_changes(round2: Path) -> None:
    original, mutated = _document_roots(round2)
    row = next(mutated.iter(w("tr")))
    row.getparent().remove(row)
    assert _collateral_outside(original, mutated, set())


@pytest.mark.parametrize(
    ("edits", "code"),
    [
        (42, "invalid_edit"),
        (["not an object"], "invalid_edit"),
        ([{"anchor": "cu_001", "delete_text": "x"}], "anchor_missing"),
        ([{"anchor": {"change_unit_id": 5, "file_sha256": "a" * 64}, "delete_text": "x"}], "anchor_missing"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": 42}], "delete_text_missing"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "insert_text": 42}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "reinstate_text": "y"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "reinstate_text": "y", "insert_text": "z"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "reinstate_text": ""}], "invalid_edit"),
    ],
)
def test_malformed_edits_fail_closed_with_error_codes(
    round2: Path, tmp_path: Path, edits, code
) -> None:
    """Loosely typed MCP input must never surface as a raw Python error."""
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), edits)
    assert err.value.code == code
    assert not list(tmp_path.iterdir())


def test_preexisting_tmp_sibling_is_not_clobbered(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "counter.docx"
    sibling = tmp_path / "counter.docx.veqtor-tmp"
    sibling.write_text("sentinel")
    apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    assert sibling.read_text() == "sentinel"
    assert out.exists()


def test_output_is_a_valid_source_for_further_rounds(round2: Path, tmp_path: Path) -> None:
    """The applied file must itself be a first-class read-path citizen."""
    out = tmp_path / "counter.docx"
    apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    listing_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    reread = extract_redlines(str(out))
    assert reread["file_sha256"] == listing_sha
    assert reread["revision_count"] == extract_redlines(str(round2))["revision_count"] + 2


def test_output_hash_failure_is_stable_and_cleans_tmp(
    round2: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_module = importlib.import_module("veqtor_docx.apply")
    original_read_bytes = Path.read_bytes
    temp_reads = {"count": 0}

    def flaky_read_bytes(self: Path) -> bytes:
        if str(self).endswith(".veqtor-tmp"):
            temp_reads["count"] += 1
            if temp_reads["count"] == 2:
                raise OSError("simulated temp read failure")
        return original_read_bytes(self)

    monkeypatch.setattr(apply_module.Path, "read_bytes", flaky_read_bytes)
    out = tmp_path / "counter.docx"

    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])

    assert err.value.code == "output_unreadable"
    assert not out.exists()
    assert not list(tmp_path.glob("*.veqtor-tmp"))
