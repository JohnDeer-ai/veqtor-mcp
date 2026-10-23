# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only revision inventory for an entire OPC package.

This is a conservative clean-export gate, not a hidden-data inspector. A known
main-body location does not promise that the decision engine supports the
particular shape. Unknown XML vocabulary, opaque parts and non-body revisions
remain explicit blockers. No source bytes, relationships or metadata change.
"""

from __future__ import annotations

import hashlib
import posixpath
from urllib.parse import unquote, urlsplit

from lxml import etree

from ._ooxml import (
    DOCUMENT_PART, W_NS, DocxError, ResourceLimitError,
    load_validated_docx, parse_xml, w,
)
from ._paragraph_edits import _PROPERTY_TAGS, _REVISION_TAGS
from .contracts import normalized_internal_package_part_name

MAX_INVENTORY_XML_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_XML_NODES = 500_000
MAX_INVENTORY_REVISIONS = 20_000
MAX_INVENTORY_FINDINGS = 20_000
MAX_INVENTORY_DEPTH = 64

_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_ODREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships/"
_XML = "http://www.w3.org/XML/1998/namespace"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16DU = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_CUSTOM_XML = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
_BIBLIOGRAPHY = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
_VTYPES = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

# Closed vocabulary is deliberately wider than the editable body grammar:
# headers, settings, styles, comments and numbering must also be inventoried.
# Unknown extension elements are never inferred to be harmless by their name.
_W_NAMES = frozenset(etree.QName(tag).localname for tag in _PROPERTY_TAGS) | frozenset("""
document body p r t delText delInstrText instrText br cr lastRenderedPageBreak
noBreakHyphen softHyphen sym ptab
tbl tr tc sectPr pgSz pgMar cols col pgNumType docGrid type headerReference
footerReference titlePg paperSrc pgBorders pgNumType footnotePr endnotePr
footnote endnote footnotes endnotes hdr ftr separator continuationSeparator
continuationNotice footnoteReference endnoteReference footnoteRef endnoteRef
numFmt numStart numRestart pos suppressEndnotes rtlGutter textDirection
formProt vAlign noEndnote lnNumType printerSettings bidi
bookmarkStart bookmarkEnd commentRangeStart commentRangeEnd commentReference proofErr
comments comment commentRef annotationRef hyperlink fldChar fldData fldSimple
smartTag smartTagPr attr sdt sdtPr sdtContent sdtEndPr alias tag id lock
placeholder docPart showingPlcHdr dataBinding temporary richText text comboBox
dropDownList listItem date dateFormat lid storeMappedDataAs calendar checkbox
checked default checkBox defaultChecked checkedState uncheckedState
picture drawing pict object altChunk txbxContent subDoc
styles style tblStylePr name aliases basedOn next link autoRedefine hidden semiHidden
unhideWhenUsed qFormat uiPriority personal personalCompose personalReply
rsid docDefaults rPrDefault pPrDefault latentStyles lsdException
numbering abstractNum abstractNumId nsid multiLevelType tmpl lvl start numFmt lvlText
lvlJc suff isLgl lvlRestart numStyleLink styleLink lvlPicBulletId numPicBullet
num lvlOverride startOverride numIdMacAtCleanup
settings writeProtection view zoom removePersonalInformation removeDateAndTime
doNotDisplayPageBoundaries displayBackgroundShape printPostScriptOverText
printFractionalCharacterWidth printFormsData embedTrueTypeFonts embedSystemFonts
saveSubsetFonts saveFormsData mirrorMargins alignBordersAndEdges bordersDoNotSurroundHeader
bordersDoNotSurroundFooter gutterAtTop hideSpellingErrors hideGrammaticalErrors
activeWritingStyle proofState formsDesign attachedTemplate linkStyles
stylePaneFormatFilter stylePaneSortMethod documentType mailMerge trackRevisions
doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride
styleLockTheme styleLockQFSet defaultTabStop autoHyphenation consecutiveHyphenLimit
hyphenationZone doNotHyphenateCaps showEnvelope summaryLength clickAndTypeStyle
defaultTableStyle evenAndOddHeaders bookFoldRevPrinting bookFoldPrinting
bookFoldPrintingSheets drawingGridHorizontalSpacing drawingGridVerticalSpacing
displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery doNotUseMarginsForDrawingGridOrigin
drawingGridHorizontalOrigin drawingGridVerticalOrigin doNotShadeFormData
noPunctuationKerning characterSpacingControl printTwoOnOne strictFirstAndLastChars
noLineBreaksAfter noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema
saveInvalidXml ignoreMixedContent alwaysShowPlaceholderText doNotDemarcateInvalidXml
saveXmlDataOnly useXSLTWhenSaving saveThroughXslt showXMLTags alwaysMergeEmptyNamespace
updateFields compat compatSetting useFELayout rsids rsidRoot mathPr attachedSchema
themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats doNotAutoCompressPictures
forceUpgrade captions caption autoCaptions autoCaption readModeInkLockDown
smartTagType schemaLibrary shapeDefaults decimalSymbol listSeparator docVars docVar
doNotEmbedSmartTags doNotSaveAsSingleFile saveSmartTagsAsXml
fontTable fonts font altName panose1 charset family notTrueType pitch sig
embedRegular embedBold embedItalic embedBoldItalic
webSettings optimizeForBrowser allowPNG relyOnVML doNotRelyOnCSS doNotSaveAsSingleFile
doNotOrganizeInFolder doNotUseLongFileNames pixelsPerInch targetScreenSz
divs div divPr blockQuote bodyDiv marLeft marRight marTop marBottom
ffData enabled calcOnExit entryMacro exitMacro helpText statusText name
textInput maxLength format default result ruby rubyPr rubyBase rt
numStyleLink styleLink label onOff
""".split())

_ACCEPTABLE_KINDS = frozenset({"ins", "del", "rPrChange", "pPrChange",
    "tcPrChange", "trPrChange", "tblPrChange", "tblGridChange", "tblPrExChange"})
_REVISION_NAMES = frozenset(etree.QName(tag).localname for tag in _REVISION_TAGS)
_WORD_CT_BASE = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_WORD_CONTENT_TYPES = frozenset(_WORD_CT_BASE + suffix + "+xml" for suffix in (
    "document.main", "styles", "settings", "fontTable", "webSettings", "numbering",
    "header", "footer", "footnotes", "endnotes", "comments", "commentsExtended",
)) | {"application/vnd.ms-word.stylesWithEffects+xml",
      "application/vnd.ms-word.commentsIds+xml",
      "application/vnd.ms-word.commentsExtensible+xml"}
_SAFE_XML_CONTENT_TYPES = _WORD_CONTENT_TYPES | frozenset({
    "application/xml", "text/xml",
    "application/vnd.openxmlformats-package.relationships+xml",
    "application/vnd.openxmlformats-package.core-properties+xml",
    "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    "application/vnd.openxmlformats-officedocument.custom-properties+xml",
    "application/vnd.openxmlformats-officedocument.theme+xml",
    "application/vnd.openxmlformats-officedocument.customXmlProperties+xml",
})
_TYPED_XML_ROOTS = {
    **{_WORD_CT_BASE + suffix + "+xml": w(root) for suffix, root in (
        ("document.main", "document"), ("styles", "styles"), ("settings", "settings"),
        ("fontTable", "fonts"), ("webSettings", "webSettings"), ("numbering", "numbering"),
        ("header", "hdr"), ("footer", "ftr"), ("footnotes", "footnotes"),
        ("endnotes", "endnotes"), ("comments", "comments"),
    )},
    _WORD_CT_BASE + "commentsExtended+xml": "{" + _W15 + "}commentsEx",
    "application/vnd.ms-word.stylesWithEffects+xml": w("styles"),
    "application/vnd.ms-word.commentsIds+xml": "{" + _W16CID + "}commentsIds",
    "application/vnd.ms-word.commentsExtensible+xml": "{" + _W16CEX + "}commentsExtensible",
    "application/vnd.openxmlformats-officedocument.customXmlProperties+xml": "{" + _CUSTOM_XML + "}datastoreItem",
    "application/vnd.openxmlformats-package.relationships+xml": "{" + _REL + "}Relationships",
}
_STANDARD_NAMESPACES = frozenset({
    _CT, _REL, _XML, W_NS,
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "http://purl.org/dc/elements/1.1/", "http://purl.org/dc/terms/",
    "http://purl.org/dc/dcmitype/", "http://www.w3.org/2001/XMLSchema-instance",
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "urn:schemas-microsoft-com:vml", "urn:schemas-microsoft-com:office:office",
    "urn:schemas-microsoft-com:office:word",
})
_BENIGN_EXTENSION_ATTRIBUTES = frozenset({
    (_MC, "Ignorable"), (_W14, "paraId"), (_W14, "textId"),
    (_W15, "collapsed"), (_W16DU, "dateUtc"),
})
_KNOWN_EXTENSION_NAMES = {
    _W14: frozenset({"docId", "defaultImageDpi"}),
    _W15: frozenset({"commentsEx", "commentEx", "docId"}),
    _W16CID: frozenset({"commentsIds", "commentId"}),
    _W16CEX: frozenset({"commentsExtensible", "commentExtensible"}),
    _CUSTOM_XML: frozenset({"datastoreItem", "schemaRefs", "schemaRef"}),
    # Native Word writes this empty bibliography store to an ordinary saved
    # fixture. Nonempty/unknown bibliography vocabulary remains unsupported.
    _BIBLIOGRAPHY: frozenset({"Sources"}),
}
_KNOWN_EXTENSION_ATTRIBUTES = {
    _W14: frozenset({"val"}),
    _W15: frozenset({"paraId", "paraIdParent", "done", "val", "restartNumberingAfterBreak"}),
    _W16CID: frozenset({"paraId", "durableId"}),
    _W16CEX: frozenset({"durableId", "dateUtc", "intelligentPlaceholder"}),
    _CUSTOM_XML: frozenset({"itemID", "uri"}),
}
_SAFE_RELATIONSHIP_TYPES = frozenset(_ODREL + name for name in (
    "officeDocument", "styles", "settings", "fontTable", "webSettings", "numbering",
    "header", "footer", "footnotes", "endnotes", "comments", "theme", "image",
    "hyperlink", "extended-properties", "custom-properties", "customXml", "customXmlProps",
)) | {_PKGREL + "metadata/core-properties",
    "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects",
    "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"}


def _limit(name, allowed, observed):
    if observed > allowed:
        raise ResourceLimitError(name, "package revision inventory exceeds its bound",
                                 allowed_count=allowed, observed_count=observed)


def _image_signature(content_type, data):
    """Only explicitly recognized, non-Office raster containers are opaque-safe."""
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/tiff":
        return data.startswith((b"II*\x00", b"MM\x00*"))
    if content_type == "image/bmp":
        return data.startswith(b"BM")
    return False


def inventory_package_revisions(payload: bytes) -> dict:
    """Inventory every member, refusing to call unsupported coverage complete.

    Malformed archives/XML and exhausted resource budgets raise the existing
    controlled DOCX errors. Semantic unsupported cases are explicit blockers.
    ``supported_location`` is a location/type fact, never an applicability claim.
    """
    if type(payload) is not bytes:
        raise DocxError("package inventory requires immutable DOCX bytes")
    package = load_validated_docx(payload, capture=None)
    if "[Content_Types].xml" not in package.parts:
        raise DocxError("DOCX package is missing [Content_Types].xml")
    blockers, revisions, parts = [], [], []
    xml_bytes = 0
    xml_nodes = 0

    def block(code, part_name, path=None):
        item = {"code": code, "part_name": part_name}
        if path is not None:
            item["element_path"] = path
        blockers.append(item)
        _limit("inventory_findings", MAX_INVENTORY_FINDINGS, len(blockers))

    def parse(name, data):
        nonlocal xml_bytes, xml_nodes
        xml_bytes += len(data)
        _limit("inventory_xml_bytes", MAX_INVENTORY_XML_BYTES, xml_bytes)
        root = parse_xml(data)
        # Existing per-part preflight already bounds attributes/namespaces.
        for node in root.iter():
            if not isinstance(node.tag, str):
                xml_nodes += 1
                continue
            parent_ns = node.getparent().nsmap if node.getparent() is not None else {}
            xml_nodes += (1 + len(node.attrib)
                          + sum(parent_ns.get(key) != value for key, value in node.nsmap.items()))
        _limit("inventory_xml_nodes", MAX_INVENTORY_XML_NODES, xml_nodes)
        return root

    types_root = parse("[Content_Types].xml", package.parts["[Content_Types].xml"])
    if types_root.tag != f"{{{_CT}}}Types":
        raise DocxError("invalid OPC content-types root")
    defaults, overrides = {}, {}
    for index, node in enumerate(types_root):
        if node.tag == f"{{{_CT}}}Default" and set(node.attrib) == {"Extension", "ContentType"}:
            key, content_type = node.get("Extension"), node.get("ContentType")
            target = defaults
            if not key or any(c in key for c in "/.\\%"):
                raise DocxError("invalid OPC default extension")
            key = key.lower()
        elif node.tag == f"{{{_CT}}}Override" and set(node.attrib) == {"PartName", "ContentType"}:
            raw = node.get("PartName", "")
            key, content_type = raw[1:], node.get("ContentType")
            target = overrides
            if not raw.startswith("/") or "%" in key or normalized_internal_package_part_name(key) != key:
                raise DocxError("invalid OPC override part name")
        else:
            block("unsupported_content_types_markup", "[Content_Types].xml", [index])
            continue
        if key in target or not content_type or content_type.strip() != content_type:
            raise DocxError("duplicate or invalid OPC content type declaration")
        target[key] = content_type
        if len(node) or (node.text or "").strip():
            block("unsupported_content_types_markup", "[Content_Types].xml", [index])
    for name in overrides:
        if name not in package.parts:
            block("missing_declared_part", name)

    for name, data in sorted(package.parts.items()):
        content_type = ("application/vnd.openxmlformats-package.content-types+xml"
                        if name == "[Content_Types].xml" else
                        overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower())))
        row = {"part_name": name, "content_type": content_type, "inspection": "uninspected"}
        parts.append(row)
        if content_type is None:
            block("missing_part_content_type", name)
        xml_like = (name == "[Content_Types].xml" or name.lower().endswith((".xml", ".rels"))
                    or bool(content_type and (content_type.endswith("+xml") or content_type in {"application/xml", "text/xml"}))
                    or data.lstrip().startswith(b"<"))
        if not xml_like:
            if _image_signature(content_type, data):
                row["inspection"] = "recognized_raster"
            else:
                block("opaque_part_uninspected", name)
            continue
        root = types_root if name == "[Content_Types].xml" else parse(name, data)
        row["inspection"] = "xml"
        if name != "[Content_Types].xml" and content_type not in _SAFE_XML_CONTENT_TYPES:
            block("unsupported_xml_content_type", name)
        if content_type in _TYPED_XML_ROOTS and root.tag != _TYPED_XML_ROOTS[content_type]:
            block("part_content_type_mismatch", name, [])
        if etree.QName(root).namespace == _BIBLIOGRAPHY:
            if (root.tag != "{" + _BIBLIOGRAPHY + "}Sources" or len(root)
                    or (root.text or "").strip()
                    or set(root.attrib) - {"SelectedStyle", "StyleName", "Version"}):
                block("unsupported_auxiliary_xml", name, [])
        if etree.QName(root).namespace == _CUSTOM_XML:
            item_tag, refs_tag, ref_tag = (
                "{" + _CUSTOM_XML + "}" + local for local in ("datastoreItem", "schemaRefs", "schemaRef"))
            if (root.tag != item_tag or set(root.attrib) != {"{" + _CUSTOM_XML + "}itemID"}
                    or len(root) > 1 or any(n.tag != refs_tag for n in root)
                    or any(n.attrib or any(c.tag != ref_tag for c in n) for n in root)
                    or any(len(n) or set(n.attrib) != {"{" + _CUSTOM_XML + "}uri"}
                           for n in root.iter(ref_tag))
                    or any((n.text or "").strip() or (n.tail or "").strip() for n in root.iter())):
                block("unsupported_auxiliary_xml", name, [])
        if (name == DOCUMENT_PART
                and content_type != _WORD_CT_BASE + "document.main+xml"):
            block("unsupported_main_content_type", name)
        if (name.lower().endswith(".rels") or content_type == "application/vnd.openxmlformats-package.relationships+xml"
                or root.tag == f"{{{_REL}}}Relationships"):
            if root.tag != f"{{{_REL}}}Relationships" or root.attrib:
                block("unsupported_relationship", name, [])
            ids = [node.get("Id") for node in root]
            if None in ids or "" in ids or len(set(ids)) != len(ids):
                block("unsupported_relationship", name, [])
            if name == "_rels/.rels":
                main_links = [node for node in root
                              if node.get("Type") == _ODREL + "officeDocument"]
                if (len(main_links) != 1
                        or main_links[0].get("Target") not in {DOCUMENT_PART, "/" + DOCUMENT_PART}
                        or main_links[0].get("TargetMode", "Internal") != "Internal"):
                    block("unsupported_main_relationship", name, [])
        body = None
        if name == DOCUMENT_PART:
            bodies = root.findall(w("body")) if root.tag == w("document") else []
            if len(bodies) != 1:
                block("invalid_document_body", name)
            else:
                body = bodies[0]
        stack = [(root, [], False)]
        while stack:
            node, path, in_body = stack.pop()
            _limit("inventory_xml_depth", MAX_INVENTORY_DEPTH, len(path))
            if not isinstance(node.tag, str):
                continue
            in_body = in_body or node is body
            qname = etree.QName(node)
            local, namespace = qname.localname, qname.namespace
            if local in _REVISION_NAMES:
                supported = (namespace == W_NS and local in _ACCEPTABLE_KINDS
                             and name == DOCUMENT_PART and in_body)
                revisions.append({"part_name": name, "element_path": path,
                                  "kind": local, "supported_location": supported})
                _limit("inventory_revisions", MAX_INVENTORY_REVISIONS, len(revisions))
                if not supported:
                    block("unsupported_revision", name, path)
            elif namespace == W_NS and local not in _W_NAMES:
                block("unknown_wordprocessing_markup", name, path)
            if node.tag in {w("delText"), w("delInstrText")} and not any(
                ancestor.tag in {w("del"), w("moveFrom"), w("conflictDel")}
                for ancestor in node.iterancestors()
            ):
                block("stray_revision_atom", name, path)
            if (namespace not in _STANDARD_NAMESPACES
                    and local not in _KNOWN_EXTENSION_NAMES.get(namespace, ())):
                block("unknown_xml_namespace", name, path)
            # Embedded content and alternate representations are not proven by
            # merely finding zero recognized revision tags in their outer XML.
            if node.tag in {w("altChunk"), w("object"), w("subDoc")}:
                block("opaque_content_reference", name, path)
            if namespace == _VTYPES and local in {
                "blob", "oblob", "stream", "ostream", "storage", "ostorage", "vstream", "cf",
            }:
                block("opaque_content_reference", name, path)
            for attribute in node.attrib:
                attr = etree.QName(attribute)
                if (attr.namespace and attr.namespace not in _STANDARD_NAMESPACES
                        and attr.namespace != _ODREL.rstrip("/")
                        and attr.localname not in _KNOWN_EXTENSION_ATTRIBUTES.get(attr.namespace, ())
                        and (attr.namespace, attr.localname) not in _BENIGN_EXTENSION_ATTRIBUTES):
                    block("unknown_attribute_namespace", name, path)
            if root.tag == f"{{{_REL}}}Relationships" and node is not root:
                _relationship(node, name, path, package.parts, block)
            for index in range(len(node) - 1, -1, -1):
                stack.append((node[index], [*path, index], in_body))
    if DOCUMENT_PART not in package.parts:
        block("missing_main_document", DOCUMENT_PART)
    if "_rels/.rels" not in package.parts:
        block("missing_package_relationships", "_rels/.rels")
    incomplete = {"opaque_part_uninspected", "unknown_xml_namespace",
                  "unknown_wordprocessing_markup", "unsupported_xml_content_type",
                  "missing_part_content_type", "unknown_attribute_namespace",
                  "opaque_content_reference", "unsupported_relationship",
                  "unsupported_external_relationship", "missing_relationship_target",
                  "unsupported_content_types_markup", "missing_declared_part",
                  "invalid_document_body", "missing_main_document"}
    incomplete |= {"unsupported_main_content_type", "missing_package_relationships",
                   "unsupported_main_relationship", "part_content_type_mismatch"}
    incomplete.add("unsupported_auxiliary_xml")
    return {"schema_version": "package_revisions.v1",
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "parts": parts, "revisions": revisions, "blockers": blockers,
            "complete": not any(item["code"] in incomplete for item in blockers),
            "clean_eligible": not blockers}


def _relationship(node, part_name, path, parts, block):
    if (node.tag != f"{{{_REL}}}Relationship" or len(node)
            or not {"Id", "Type", "Target"} <= set(node.attrib)
            or set(node.attrib) - {"Id", "Type", "Target", "TargetMode"}):
        block("unsupported_relationship", part_name, path)
        return
    kind, target = node.get("Type"), node.get("Target")
    if kind not in _SAFE_RELATIONSHIP_TYPES:
        block("unsupported_relationship", part_name, path)
    mode = node.get("TargetMode", "Internal")
    if mode == "External":
        if kind != _ODREL + "hyperlink":
            block("unsupported_external_relationship", part_name, path)
        return
    if mode != "Internal":
        block("unsupported_relationship", part_name, path)
        return
    try:
        split = urlsplit(target)
    except ValueError:
        block("unsupported_relationship", part_name, path)
        return
    if split.scheme or split.netloc or split.query or split.fragment or "\\" in target:
        block("unsupported_relationship", part_name, path)
        return
    source = "" if part_name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(part_name))
    decoded = unquote(split.path)
    resolved = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/")
                                  else posixpath.join(source, decoded))
    if normalized_internal_package_part_name(resolved) != resolved or resolved not in parts:
        block("missing_relationship_target", part_name, path)
