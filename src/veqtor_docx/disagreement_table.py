# SPDX-License-Identifier: Apache-2.0
"""Deterministic, new DOCX containing only an explicitly selected public view."""
from copy import deepcopy
from io import BytesIO
import zipfile

from lxml import etree as E

from .round_snapshots import SnapshotError

COLUMNS = ("sent_quote", "incoming_quote", "proposed_wording")
LABELS = {
    "ru": ("Отправленная редакция", "Входящая редакция", "Предлагаемая редакция"),
    "en": ("Sent wording", "Received wording", "Proposed wording"),
}
MISSING = {"ru": "Не записано", "en": "Not recorded"}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _fail():
    raise SnapshotError("invalid_disagreement_table", "select bounded plain-text table fields")


def _text(value, limit):
    if (type(value) is not str or not 1 <= len(value) <= limit
            or any(not (c in "\t\n" or 0x20 <= ord(c) <= 0xD7FF
                        or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
                   for c in value)):
        _fail()


def validate(view):
    """Return an owned copy; no extension fields or private issue data accepted."""
    if type(view) is not dict or set(view) != {"name", "title", "language", "columns", "rows"}:
        _fail()
    _text(view["name"], 120)
    _text(view["title"], 200)
    if type(view["language"]) is not str or view["language"] not in LABELS:
        _fail()
    cols, rows = view["columns"], view["rows"]
    if (type(cols) is not list or not 1 <= len(cols) <= 3
            or any(type(c) is not str or c not in COLUMNS for c in cols)
            or len(set(cols)) != len(cols) or type(rows) is not list or not 1 <= len(rows) <= 20):
        _fail()
    for row in rows:
        if type(row) is not dict or set(row) != set(cols):
            _fail()
        for value in row.values():
            if value is not None:
                _text(value, 8000)
    return deepcopy(view)


def _el(parent, name, **attrs):
    return E.SubElement(parent, f"{{{W}}}{name}", {f"{{{W}}}{k}": str(v) for k, v in attrs.items()})


def _paragraph(parent, text, *, bold=False, style=None):
    p = _el(parent, "p")
    pp = _el(p, "pPr")
    if style:
        _el(pp, "pStyle", val=style)
    _el(pp, "spacing", after=100)
    r = _el(p, "r")
    if bold:
        _el(_el(r, "rPr"), "b")
    # XML cannot round-trip CR; validation refuses it instead of silently
    # normalizing a legal quotation. Newlines/tabs retain their exact meaning.
    chunk = ""
    for char in text + "\0":
        if char in "\n\t\0":
            if chunk:
                node = _el(r, "t")
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                node.text = chunk
                chunk = ""
            if char != "\0":
                _el(r, "br" if char == "\n" else "tab")
        else:
            chunk += char
    return p


def render(view):
    """No source/template metadata, comments, fields, links or hidden text."""
    view = validate(view)
    doc = E.Element(f"{{{W}}}document", nsmap={"w": W})
    body = _el(doc, "body")
    _paragraph(body, view["title"], style="Title")
    tbl = _el(body, "tbl")
    props = _el(tbl, "tblPr")
    _el(props, "tblW", w=9638, type="dxa")
    _el(props, "tblLayout", type="fixed")
    borders = _el(props, "tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        _el(borders, edge, val="single", sz=4, color="B8C2CC")
    margins = _el(props, "tblCellMar")
    for edge in ("top", "left", "bottom", "right"):
        _el(margins, edge, w=100, type="dxa")
    width = 9638 // len(view["columns"])
    grid = _el(tbl, "tblGrid")
    for _ in view["columns"]:
        _el(grid, "gridCol", w=width)
    headers = {key: LABELS[view["language"]][COLUMNS.index(key)] for key in view["columns"]}
    for index, row in enumerate([headers, *view["rows"]]):
        tr = _el(tbl, "tr")
        if index == 0:
            _el(_el(tr, "trPr"), "tblHeader")
        # Data rows deliberately split across pages: even the maximum supported
        # legal clause must remain visible rather than become an oversized row.
        for key in view["columns"]:
            tc = _el(tr, "tc")
            tcp = _el(tc, "tcPr")
            _el(tcp, "tcW", w=width, type="dxa")
            if index == 0:
                _el(tcp, "shd", fill="EAF0F5")
            _paragraph(tc, row[key] if row[key] is not None else MISSING[view["language"]], bold=index == 0)
    sect = _el(body, "sectPr")
    _el(sect, "pgSz", w=11906, h=16838)
    _el(sect, "pgMar", top=1134, right=1134, bottom=1134, left=1134, header=0, footer=0, gutter=0)
    styles = E.Element(f"{{{W}}}styles", nsmap={"w": W})
    default = _el(styles, "docDefaults")
    rp = _el(_el(default, "rPrDefault"), "rPr")
    _el(rp, "rFonts", ascii="Arial", hAnsi="Arial", eastAsia="Arial", cs="Arial")
    _el(rp, "sz", val=22)
    _el(rp, "color", val="000000")
    title = _el(styles, "style", type="paragraph", styleId="Title")
    _el(title, "name", val="Title")
    _el(_el(title, "pPr"), "keepNext")
    rp = _el(title, "rPr")
    _el(rp, "sz", val=32)
    _el(rp, "b")
    _el(rp, "color", val="000000")
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "word/document.xml": E.tostring(doc, encoding="UTF-8", xml_declaration=True, standalone=True),
        "word/styles.xml": E.tostring(styles, encoding="UTF-8", xml_declaration=True, standalone=True),
    }
    result = BytesIO()
    with zipfile.ZipFile(result, "w") as package:
        for name, payload in sorted(parts.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            package.writestr(info, payload)
    return result.getvalue()
