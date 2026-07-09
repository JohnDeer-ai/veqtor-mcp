# SPDX-License-Identifier: Apache-2.0
"""Deterministic generator for synthetic DOCX negotiation rounds.

Public fixtures must be synthetic and script-generated (see README Day-0
invariants), so tests and the demo build their corpus by calling
:func:`generate_demo_rounds` instead of shipping binary files.

The generated markup deliberately mimics real Word output rather than the
cleanest possible XML: custom legal paragraph styles with ``outlineLvl``
(no built-in ``Heading1-9``), style-level ``numPr`` numbering, runs split
mid-sentence inside a single tracked change, adjacent same-author ``w:ins``
wrappers, ``rsid`` attribute noise, revisions inside table cells, and
formatting/move revisions that M1 reports as unsupported.

Storyline across rounds (the demo question is "what happened to the
limitation of liability?"):

- round 1: clean outgoing draft, liability cap = trailing 12 months fees.
- round 2 (counterparty, author ``53``): cap replaced with USD 50,000, an
  audit sentence deleted, an adviser-disclosure sentence inserted, one
  cancellation-fee table cell changed, one formatting-only change.
- round 3 (our counsel, author ``A. Petrov``): cap replaced with 150% of the
  affected Work Order, a carve-out sentence inserted, a new manually numbered
  clause 9.5 inserted, one sentence moved, one paragraph-format change.
- round 4 (counterparty, author ``53``): cap replaced with trailing 12 months
  Work Order fees, the willful-misconduct carve-out deleted.

Every byte is deterministic: fixed dates, fixed revision ids, fixed zip
metadata. Generating twice yields identical files and hashes.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from ._ooxml import W_NS, w

_XML_DECL = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
_ZIP_DATE = (2026, 1, 5, 12, 0, 0)

# ---------------------------------------------------------------------------
# Static package parts
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_ROOT_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_DOCUMENT_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>"""

_APP_PROPS = """<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<Application>Veqtor Synthetic Fixtures</Application>
</Properties>"""


def _core_props(created: str, modified: str) -> str:
    return (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title>Master Development and Supply Agreement (synthetic)</dc:title>"
        "<dc:creator>Veqtor Synthetic Fixtures</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified>'
        "</cp:coreProperties>"
    )


# Custom legal styles on purpose: real firm templates rarely use Heading1-9,
# so anchors must come from outlineLvl/numbering, not style names.
_STYLES = f"""<w:styles xmlns:w="{W_NS}">
<w:style w:type="paragraph" w:default="1" w:styleId="VBody">
<w:name w:val="V Body Text"/>
<w:pPr><w:spacing w:after="120"/></w:pPr>
</w:style>
<w:style w:type="paragraph" w:styleId="VLegalTitle">
<w:name w:val="V Legal Title"/>
<w:basedOn w:val="VBody"/>
<w:pPr><w:jc w:val="center"/></w:pPr>
<w:rPr><w:b/><w:caps/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="VLegal1">
<w:name w:val="V Legal Level 1"/>
<w:basedOn w:val="VBody"/>
<w:pPr>
<w:keepNext/>
<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>
<w:outlineLvl w:val="0"/>
</w:pPr>
<w:rPr><w:b/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="VLegal2">
<w:name w:val="V Legal Level 2"/>
<w:basedOn w:val="VLegal1"/>
<w:pPr>
<w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>
<w:outlineLvl w:val="1"/>
</w:pPr>
</w:style>
<w:style w:type="paragraph" w:styleId="VLegalManual">
<w:name w:val="V Legal Manual Number"/>
<w:basedOn w:val="VBody"/>
<w:pPr><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="VList">
<w:name w:val="V List Item"/>
<w:basedOn w:val="VBody"/>
<w:pPr><w:numPr><w:ilvl w:val="2"/><w:numId w:val="1"/></w:numPr></w:pPr>
</w:style>
</w:styles>"""

_NUMBERING = f"""<w:numbering xmlns:w="{W_NS}">
<w:abstractNum w:abstractNumId="10">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>
<w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%3)"/></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
</w:numbering>"""

# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


@dataclass
class Seg:
    """One run-level segment of a paragraph."""

    kind: str  # text | ins | del | moveFrom | moveTo | rprchange
    text: str
    split: bool = False  # emit as two adjacent same-author wrappers


def T(text: str) -> Seg:
    return Seg("text", text)


def INS(text: str, split: bool = False) -> Seg:
    return Seg("ins", text, split=split)


def DEL(text: str) -> Seg:
    return Seg("del", text)


@dataclass
class Para:
    style: str
    segments: list[Seg] = field(default_factory=list)
    ppr_change: bool = False  # paragraph formatting changed (unsupported in M1)
    inserted: bool = False  # whole paragraph inserted (adds a para-mark w:ins)


@dataclass
class TableRow:
    cells: list[list[Seg]]  # cells -> segments (one paragraph per cell)
    inserted: bool = False  # whole row tracked as inserted (w:trPr/w:ins)


@dataclass
class Table:
    rows: list[TableRow]


@dataclass
class RoundSpec:
    filename: str
    author: str
    date: str
    core_created: str
    core_modified: str
    blocks: list[Para | Table]


# ---------------------------------------------------------------------------
# XML emission
# ---------------------------------------------------------------------------


class _DocBuilder:
    def __init__(self, spec: RoundSpec) -> None:
        self.spec = spec
        self._rev_id = 100  # Word-style arbitrary but stable revision ids
        self._rsid = 0x00A31000

    def _next_id(self) -> str:
        self._rev_id += 1
        return str(self._rev_id)

    def _next_rsid(self) -> str:
        self._rsid = (self._rsid + 0x1111) & 0xFFFFFF
        return f"{self._rsid:08X}"

    def _run(self, text: str, deleted: bool = False, bold: bool = False) -> etree._Element:
        run = etree.Element(w("r"))
        run.set(w("rsidRPr"), self._next_rsid())
        if bold:
            rpr = etree.SubElement(run, w("rPr"))
            etree.SubElement(rpr, w("b"))
        t = etree.SubElement(run, w("delText") if deleted else w("t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = text
        return run

    def _wrapper(self, tag: str, text: str, deleted: bool, split_runs: bool) -> etree._Element:
        el = etree.Element(w(tag))
        el.set(w("id"), self._next_id())
        el.set(w("author"), self.spec.author)
        el.set(w("date"), self.spec.date)
        if split_runs and len(text) > 4:
            # Word routinely splits one logical edit across several runs.
            mid = len(text) // 2
            el.append(self._run(text[:mid], deleted=deleted))
            el.append(self._run(text[mid:], deleted=deleted))
        else:
            el.append(self._run(text, deleted=deleted))
        return el

    def _emit_segment(self, para: etree._Element, seg: Seg) -> None:
        if seg.kind == "text":
            para.append(self._run(seg.text))
        elif seg.kind == "ins":
            if seg.split:
                # Two adjacent same-author w:ins wrappers: one logical edit,
                # two revision elements. Extraction must merge them.
                mid = seg.text.rfind(" ", 0, len(seg.text) * 2 // 3)
                mid = mid if mid > 0 else len(seg.text) // 2
                para.append(self._wrapper("ins", seg.text[:mid], False, split_runs=True))
                para.append(self._wrapper("ins", seg.text[mid:], False, split_runs=False))
            else:
                para.append(self._wrapper("ins", seg.text, False, split_runs=False))
        elif seg.kind == "del":
            para.append(self._wrapper("del", seg.text, True, split_runs=False))
        elif seg.kind in ("moveFrom", "moveTo"):
            para.append(self._wrapper(seg.kind, seg.text, False, split_runs=False))
        elif seg.kind == "rprchange":
            # Formatting-only tracked change: run is bold now, rPrChange
            # records the previous plain formatting.
            run = self._run(seg.text, bold=True)
            rpr = run.find(w("rPr"))
            change = etree.SubElement(rpr, w("rPrChange"))
            change.set(w("id"), self._next_id())
            change.set(w("author"), self.spec.author)
            change.set(w("date"), self.spec.date)
            etree.SubElement(change, w("rPr"))
            para.append(run)
        else:  # pragma: no cover - generator misuse
            raise ValueError(f"unknown segment kind: {seg.kind}")

    def _paragraph(self, model: Para) -> etree._Element:
        para = etree.Element(w("p"))
        para.set(w("rsidR"), self._next_rsid())
        para.set(w("rsidRDefault"), self._next_rsid())
        ppr = etree.SubElement(para, w("pPr"))
        style = etree.SubElement(ppr, w("pStyle"))
        style.set(w("val"), model.style)
        if model.inserted:
            # Word marks the paragraph mark of an inserted paragraph too.
            rpr = etree.SubElement(ppr, w("rPr"))
            mark = etree.SubElement(rpr, w("ins"))
            mark.set(w("id"), self._next_id())
            mark.set(w("author"), self.spec.author)
            mark.set(w("date"), self.spec.date)
        if model.ppr_change:
            jc = etree.SubElement(ppr, w("jc"))
            jc.set(w("val"), "both")
            change = etree.SubElement(ppr, w("pPrChange"))
            change.set(w("id"), self._next_id())
            change.set(w("author"), self.spec.author)
            change.set(w("date"), self.spec.date)
            etree.SubElement(change, w("pPr"))
        for seg in model.segments:
            self._emit_segment(para, seg)
        return para

    def _table(self, model: Table) -> etree._Element:
        tbl = etree.Element(w("tbl"))
        tblpr = etree.SubElement(tbl, w("tblPr"))
        borders = etree.SubElement(tblpr, w("tblBorders"))
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = etree.SubElement(borders, w(edge))
            e.set(w("val"), "single")
            e.set(w("sz"), "4")
        for row in model.rows:
            tr = etree.SubElement(tbl, w("tr"))
            trpr = etree.SubElement(tr, w("trPr"))
            if row.inserted:
                # Word marks an inserted row structurally in trPr, in
                # addition to wrapping the cell content in w:ins.
                mark = etree.SubElement(trpr, w("ins"))
                mark.set(w("id"), self._next_id())
                mark.set(w("author"), self.spec.author)
                mark.set(w("date"), self.spec.date)
            for cell in row.cells:
                tc = etree.SubElement(tr, w("tc"))
                etree.SubElement(tc, w("tcPr"))
                tc.append(
                    self._paragraph(
                        Para("VBody", segments=cell, inserted=row.inserted)
                    )
                )
        return tbl

    def build(self) -> bytes:
        root = etree.Element(w("document"), nsmap={"w": W_NS})
        body = etree.SubElement(root, w("body"))
        for block in self.spec.blocks:
            if isinstance(block, Table):
                body.append(self._table(block))
            else:
                body.append(self._paragraph(block))
        sectpr = etree.SubElement(body, w("sectPr"))
        pgsz = etree.SubElement(sectpr, w("pgSz"))
        pgsz.set(w("w"), "11906")
        pgsz.set(w("h"), "16838")
        return etree.tostring(root)


def _write_docx(path: Path, spec: RoundSpec) -> None:
    parts: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", _CONTENT_TYPES.encode()),
        ("_rels/.rels", _ROOT_RELS.encode()),
        ("word/document.xml", _DocBuilder(spec).build()),
        ("word/_rels/document.xml.rels", _DOCUMENT_RELS.encode()),
        ("word/styles.xml", _STYLES.encode()),
        ("word/numbering.xml", _NUMBERING.encode()),
        ("docProps/core.xml", _core_props(spec.core_created, spec.core_modified).encode()),
        ("docProps/app.xml", _APP_PROPS.encode()),
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, _XML_DECL + data)


# ---------------------------------------------------------------------------
# The synthetic agreement
# ---------------------------------------------------------------------------

CAP_R1 = (
    "the total fees paid by Client under this Agreement during the twelve (12) "
    "months preceding the first event giving rise to liability"
)
CAP_R2 = "USD 50,000"
CAP_R3 = "one hundred fifty percent (150%) of the fees paid under the affected Work Order"
CAP_R4 = (
    "the total fees paid under the affected Work Order during the twelve (12) "
    "months preceding the first event giving rise to liability"
)

AUDIT_SENTENCE = " Client may conduct such an audit no more than twice in any calendar year."
ADVISER_SENTENCE = (
    " The Receiving Party may disclose Confidential Information to its professional "
    "advisers bound by equivalent confidentiality obligations."
)
CARVEOUT_KEPT = " The foregoing cap shall not apply to breaches of Clause 9 (Confidentiality)"
CARVEOUT_DROPPED = " or to a Party's willful misconduct"
COMPELLED_CLAUSE = (
    "9.5 Compelled Disclosure. A Party may disclose Confidential Information where "
    "required by law or by an order of a court of competent jurisdiction, provided "
    "that it gives the other Party prompt written notice where lawful."
)
TITLE_SENTENCE = " Title to each Batch passes to Client upon payment in full."
EXPRESS_ROW_LABEL = "Same-day cancellation"
EXPRESS_ROW_FEE = "110%"

AUTHOR_CP = "53"  # counterparty exports a bare numeric author string
AUTHOR_US = "A. Petrov"


def _contract_blocks(
    *,
    cap_body: list[Seg],
    audit_tail: list[Seg],
    adviser: list[Seg],
    cancel_fee_cell: list[Seg],
    extra_cancel_row: TableRow | None,
    compelled_para: Para | None,
    governing_law: list[Seg],
    delivery_p1_tail: list[Seg],
    delivery_p2_tail: list[Seg],
    notices_ppr_change: bool,
) -> list[Para | Table]:
    """Assemble the full agreement with round-specific segments patched in."""

    def sec(title: str) -> Para:
        return Para("VLegal1", [T(title)])

    def sub(title: str) -> Para:
        return Para("VLegal2", [T(title)])

    def body(text: str) -> Para:
        return Para("VBody", [T(text)])

    blocks: list[Para | Table] = [
        Para("VLegalTitle", [T("Master Development and Supply Agreement")]),
        body(
            "This Master Development and Supply Agreement (the “Agreement”) is "
            "entered into as of 3 February 2026 by and between Aurora Biologics Ltd., a "
            "company registered in England (“Client”), and Meridian Manufacturing "
            "GmbH, a company registered in Germany (“Contractor”)."
        ),
        sec("Definitions"),
        body(
            "“Batch” means a discrete production run of the Product. “Work "
            "Order” means a written order for Services agreed under this Agreement. "
            "“Confidential Information” has the meaning given in Clause 9."
        ),
        sec("Scope of Services"),
        body(
            "Contractor shall perform the development and manufacturing services described "
            "in each Work Order (the “Services”) in accordance with this Agreement, "
            "Applicable Law and cGMP."
        ),
        sec("Fees and Payment"),
        sub("Fees"),
        body("Client shall pay the fees set out in the applicable Work Order."),
        sub("Invoicing and Payment"),
        body(
            "Contractor shall invoice on the milestones stated in the Work Order. Undisputed "
            "invoices are payable within thirty (30) days of receipt."
        ),
        sub("Cancellation Charges"),
        body(
            "If Client cancels a scheduled production slot, the following cancellation fee "
            "applies:"
        ),
        Table(
            [
                TableRow([[T("Notice period before the scheduled production slot")], [T("Cancellation fee (% of the Work Order value)")]]),
                TableRow([[T("60 days or more")], [T("0%")]]),
                TableRow([[T("30 to 59 days")], cancel_fee_cell]),
                TableRow([[T("15 to 29 days")], [T("75%")]]),
                TableRow([[T("Less than 15 days")], [T("100%")]]),
                *([extra_cancel_row] if extra_cancel_row is not None else []),
            ]
        ),
        sec("Delivery"),
        Para("VBody", [T(
            "Contractor shall deliver each Batch FCA (Incoterms 2020) Contractor's facility "
            "in Hamburg, Germany, unless the Work Order states otherwise."
        ), *delivery_p1_tail]),
        Para("VBody", [T(
            "Risk in each Batch passes to Client upon handover to the first carrier."
        ), *delivery_p2_tail]),
        sec("Acceptance"),
        body(
            "Client may reject a Batch that fails to conform to the Specifications by notice "
            "within thirty (30) days of delivery."
        ),
        sec("Warranties"),
        body(
            "Contractor warrants that the Services will be performed with reasonable skill "
            "and care by suitably qualified personnel."
        ),
        sec("Records and Audit"),
        Para("VBody", [T(
            "Contractor shall keep complete and accurate records of the Services. Client "
            "may, upon ten (10) Business Days' notice, audit Contractor's records relating "
            "to the Services during normal business hours."
        ), *audit_tail]),
        sec("Intellectual Property"),
        body(
            "All results generated by Contractor in the performance of the Services shall be "
            "owned by Client upon payment of the corresponding fees."
        ),
        sec("Confidentiality"),
        sub("Confidentiality Obligations"),
        Para("VBody", [T(
            "Each Party shall keep the other Party's Confidential Information strictly "
            "confidential and use it solely for the performance of this Agreement."
        ), *adviser]),
        sub("Exclusions"),
        body(
            "Confidential Information does not include information that is or becomes public "
            "other than through breach of this Agreement."
        ),
        sub("Return of Information"),
        body(
            "Upon termination each Party shall return or destroy the other Party's "
            "Confidential Information, save for archival copies required by law."
        ),
        sub("Injunctive Relief"),
        body(
            "Each Party acknowledges that damages may be an inadequate remedy for breach of "
            "this Clause 9 and that injunctive relief may be sought."
        ),
    ]
    if compelled_para is not None:
        blocks.append(compelled_para)
    blocks += [
        sec("Data Protection"),
        body(
            "Each Party shall comply with Applicable Law relating to the protection of "
            "personal data in connection with this Agreement."
        ),
        sec("Insurance"),
        body(
            "Contractor shall maintain insurance cover appropriate to its obligations under "
            "this Agreement with reputable insurers."
        ),
        sec("Term and Termination"),
        body("Either Party may terminate this Agreement with immediate effect by notice if:"),
        Para("VList", [T("the other Party commits a material breach that is not remedied within thirty (30) days of notice;")]),
        Para("VList", [T("the other Party becomes insolvent or subject to analogous proceedings; or")]),
        Para("VList", [T("performance is prevented by an event of Force Majeure lasting more than ninety (90) days.")]),
        sec("Indemnities"),
        body(
            "Contractor shall indemnify Client against third-party claims arising from "
            "Contractor's negligence or willful misconduct in performing the Services."
        ),
        sec("Liability"),
        sub("Exclusions"),
        body(
            "Neither Party is liable for loss of profit, loss of business or indirect or "
            "consequential loss."
        ),
        sub("Limitation of Liability"),
        Para("VBody", cap_body),
        sub("Exceptions"),
        body(
            "Nothing in this Agreement excludes or limits liability for death or personal "
            "injury caused by negligence, or for fraud."
        ),
        sec("Governing Law and Disputes"),
        Para("VBody", governing_law),
        sec("Notices"),
        Para(
            "VBody",
            [T(
                "Notices under this Agreement must be in writing and delivered by hand, by "
                "courier or by email to the addresses stated in the Work Order."
            )],
            ppr_change=notices_ppr_change,
        ),
    ]
    return blocks


def _cap_sentence(*middle: Seg) -> list[Seg]:
    return [
        T(
            "Except as set out in Clause 14.3, each Party's total aggregate liability under "
            "this Agreement shall not exceed "
        ),
        *middle,
    ]


GOVERNING_LAW_PLAIN = [T(
    "This Agreement is governed by the laws of England and Wales. Disputes shall be "
    "finally resolved by the courts of England."
)]


def _round_specs() -> list[RoundSpec]:
    round1 = RoundSpec(
        filename="round-1-outgoing-draft.docx",
        author="",  # no tracked changes in the clean draft
        date="",
        core_created="2026-04-14T09:00:00Z",
        core_modified="2026-04-14T09:00:00Z",
        blocks=_contract_blocks(
            cap_body=_cap_sentence(T(CAP_R1), T(" in respect of all claims in aggregate.")),
            audit_tail=[T(AUDIT_SENTENCE)],
            adviser=[],
            cancel_fee_cell=[T("50%")],
            extra_cancel_row=None,
            compelled_para=None,
            governing_law=GOVERNING_LAW_PLAIN,
            delivery_p1_tail=[],
            delivery_p2_tail=[T(TITLE_SENTENCE)],
            notices_ppr_change=False,
        ),
    )

    round2 = RoundSpec(
        filename="round-2-counterparty-redline.docx",
        author=AUTHOR_CP,
        date="2026-05-05T09:30:00Z",
        core_created="2026-04-14T09:00:00Z",
        core_modified="2026-05-05T09:31:00Z",
        blocks=_contract_blocks(
            # Replace: adjacent del + ins, one logical change unit.
            cap_body=_cap_sentence(
                DEL(CAP_R1), INS(CAP_R2), T(" in respect of all claims in aggregate.")
            ),
            audit_tail=[DEL(AUDIT_SENTENCE)],
            adviser=[INS(ADVISER_SENTENCE, split=True)],
            # The percent sign stays plain: a table-cell paragraph with both a
            # tracked change and untouched text, so the write path can anchor
            # an edit inside a table.
            cancel_fee_cell=[DEL("50"), INS("65"), T("%")],
            # Whole tracked row insertion: trPr/ins + inserted cell content.
            extra_cancel_row=TableRow(
                [[INS(EXPRESS_ROW_LABEL)], [INS(EXPRESS_ROW_FEE)]], inserted=True
            ),
            compelled_para=None,
            governing_law=[
                T("This Agreement is governed by the laws of "),
                Seg("rprchange", "England and Wales"),
                T(". Disputes shall be finally resolved by the courts of England."),
            ],
            delivery_p1_tail=[],
            delivery_p2_tail=[T(TITLE_SENTENCE)],
            notices_ppr_change=False,
        ),
    )

    round3 = RoundSpec(
        filename="round-3-our-counter.docx",
        author=AUTHOR_US,
        date="2026-05-19T16:05:00Z",
        core_created="2026-04-14T09:00:00Z",
        core_modified="2026-05-19T16:06:00Z",
        blocks=_contract_blocks(
            cap_body=_cap_sentence(
                DEL(CAP_R2),
                INS(CAP_R3),
                T(" in respect of all claims in aggregate."),
                INS(CARVEOUT_KEPT + CARVEOUT_DROPPED + "."),
            ),
            audit_tail=[],
            adviser=[T(ADVISER_SENTENCE)],
            cancel_fee_cell=[T("65%")],
            extra_cancel_row=TableRow([[T(EXPRESS_ROW_LABEL)], [T(EXPRESS_ROW_FEE)]]),
            compelled_para=Para(
                "VLegalManual", [INS(COMPELLED_CLAUSE)], inserted=True
            ),
            governing_law=GOVERNING_LAW_PLAIN,
            delivery_p1_tail=[Seg("moveTo", TITLE_SENTENCE)],
            delivery_p2_tail=[Seg("moveFrom", TITLE_SENTENCE)],
            notices_ppr_change=True,
        ),
    )

    round4 = RoundSpec(
        filename="round-4-counterparty-reply.docx",
        author=AUTHOR_CP,
        date="2026-06-02T11:20:00Z",
        core_created="2026-04-14T09:00:00Z",
        core_modified="2026-06-02T11:21:00Z",
        blocks=_contract_blocks(
            cap_body=_cap_sentence(
                DEL(CAP_R3),
                INS(CAP_R4),
                T(" in respect of all claims in aggregate."),
                T(CARVEOUT_KEPT),
                DEL(CARVEOUT_DROPPED),
                T("."),
            ),
            audit_tail=[],
            adviser=[T(ADVISER_SENTENCE)],
            cancel_fee_cell=[T("65%")],
            extra_cancel_row=TableRow([[T(EXPRESS_ROW_LABEL)], [T(EXPRESS_ROW_FEE)]]),
            compelled_para=Para("VLegalManual", [T(COMPELLED_CLAUSE)]),
            governing_law=GOVERNING_LAW_PLAIN,
            delivery_p1_tail=[T(TITLE_SENTENCE)],
            delivery_p2_tail=[],
            notices_ppr_change=False,
        ),
    )
    return [round1, round2, round3, round4]


def generate_demo_rounds(out_dir: str | Path) -> list[Path]:
    """Write the four synthetic negotiation rounds into ``out_dir``.

    Returns the written paths in round order. Output is byte-deterministic.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for spec in _round_specs():
        path = out / spec.filename
        _write_docx(path, spec)
        written.append(path)
    return written


def main() -> None:
    """Console entry point: write the demo rounds to the given folder."""
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "veqtor-demo-rounds"
    for p in generate_demo_rounds(target):
        print(p)


if __name__ == "__main__":  # pragma: no cover
    main()
