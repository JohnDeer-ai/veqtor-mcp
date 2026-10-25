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
import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

from lxml import etree

from ._ooxml import (
    DOCUMENT_PART, W_NS, DocxError, ResourceLimitError,
    load_validated_docx, parse_xml, w,
)
from ._paragraph_edits import _PROPERTY_TAGS, _REVISION_TAGS
from .contracts import normalized_internal_package_part_name
from ._sharepoint_metadata import sharepoint_schema_model

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
_THEME_2012 = "http://schemas.microsoft.com/office/thememl/2012/main"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
_THEME_URI = "{05A4C25C-085E-4340-85A3-A5531E510DB2}"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_CUSTOM_XML = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
_BIBLIOGRAPHY = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
_VTYPES = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_SP_FORMS = "http://schemas.microsoft.com/sharepoint/v3/contenttype/forms"

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
      "application/vnd.ms-word.commentsExtensible+xml",
      _WORD_CT_BASE + "commentsIds+xml", _WORD_CT_BASE + "commentsExtensible+xml",
      _WORD_CT_BASE + "people+xml"}
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
    "application/vnd.openxmlformats-officedocument.theme+xml": "{" + _DRAWING + "}theme",
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
    _WORD_CT_BASE + "commentsIds+xml": "{" + _W16CID + "}commentsIds",
    _WORD_CT_BASE + "commentsExtensible+xml": "{" + _W16CEX + "}commentsExtensible",
    _WORD_CT_BASE + "people+xml": "{" + _W15 + "}people",
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
# Namespace recognition alone does not prove a supported schema: arbitrary
# custom XML can borrow a standard namespace and carry opaque future content.
_STANDARD_ELEMENT_NAMES = {
    _CT: frozenset("Types Default Override".split()),
    _REL: frozenset("Relationships Relationship".split()),
    _XML: frozenset(),
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties": frozenset(
        "coreProperties category contentStatus contentType keywords lastModifiedBy lastPrinted revision version".split()),
    "http://purl.org/dc/elements/1.1/": frozenset("title subject creator description identifier language".split()),
    "http://purl.org/dc/terms/": frozenset("created modified".split()),
    "http://purl.org/dc/dcmitype/": frozenset(),
    "http://www.w3.org/2001/XMLSchema-instance": frozenset(),
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties": frozenset(
        "Properties Template Manager Company Pages Words Characters PresentationFormat Lines Paragraphs Slides Notes TotalTime HiddenSlides MMClips ScaleCrop HeadingPairs TitlesOfParts LinksUpToDate CharactersWithSpaces SharedDoc HyperlinkBase HLinks HyperlinksChanged DigSig Application AppVersion DocSecurity".split()),
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties": frozenset({"Properties", "property"}),
    _VTYPES: frozenset(
        "vector array variant empty null i1 i2 i4 i8 int ui1 ui2 ui4 ui8 uint r4 r8 decimal lpstr lpwstr bstr date filetime bool cy error clsid blob oblob stream ostream storage ostorage vstream cf".split()),
    "http://schemas.openxmlformats.org/drawingml/2006/main": frozenset("""
        accent1 accent2 accent3 accent4 accent5 accent6 alpha alphaMod alphaOff
        bevelT bevelB bgFillStyleLst bodyPr camera clrScheme clrMap cs dk1 dk2 ea
        effectLst effectRef effectStyle effectStyleLst extraClrSchemeLst fillRef
        fillStyleLst fillToRect fmtScheme folHlink font fontRef fontScheme gradFill
        gs gsLst hlink latin lightRig lin ln lnDef lnRef lnStyleLst lstStyle lt1 lt2
        majorFont minorFont objectDefaults outerShdw path prstDash rot satMod
        scene3d schemeClr shade solidFill sp3d spDef spPr srgbClr style sysClr
        theme themeElements themeOverride tint lumMod miter extLst noFill blip blipFill srcRect
        stretch tile xfrm off ext chOff chExt prstGeom avLst custGeom gdLst gd
        ahLst cxnLst rect pathLst moveTo lnTo close arcTo cubicBezTo quadBezTo pt
        graphic graphicData graphicFrameLocks picLocks hlinkClick hlinkHover
        buNone buChar buAutoNum p pPr r rPr t endParaRPr defRPr txBody
    """.split()),
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing": frozenset("""
        inline anchor simplePos positionH positionV align posOffset extent
        effectExtent wrapNone wrapSquare wrapTight wrapThrough wrapTopAndBottom
        wrapPolygon start lineTo docPr cNvGraphicFramePr
    """.split()),
    "http://schemas.openxmlformats.org/drawingml/2006/picture": frozenset(
        "pic nvPicPr cNvPr cNvPicPr blipFill spPr".split()),
    "http://schemas.openxmlformats.org/officeDocument/2006/math": frozenset("""
        mathPr mathFont brkBin brkBinSub defJc dispDef intLim lMargin naryLim rMargin
        smallFrac wrapIndent oMath oMathPara oMathParaPr r rPr t ctrlPr f fPr num den
        rad radPr deg e d dPr begChr endChr sepChr grow nary naryPr chr limLoc
        sub sup sSub sSubPr sSup sSupPr sSubSup sSubSupPr limLow limLowPr limUpp
        limUppPr func funcPr fName acc accPr bar barPr box boxPr borderBox borderBoxPr
        groupChr groupChrPr eqArr eqArrPr m mPr mr mc mcPr mcs count baseJc
    """.split()),
    "urn:schemas-microsoft-com:vml": frozenset(
        "shape shapetype group textbox fill stroke shadow path formulas f handles h imagedata textpath rect roundrect oval line polyline arc curve image background".split()),
    "urn:schemas-microsoft-com:office:office": frozenset(
        "shapedefaults shapelayout idmap lock signatureline skew extrusion callout clippath".split()),
    "urn:schemas-microsoft-com:office:word": frozenset(
        "wrap anchorlock bordertop borderleft borderbottom borderright".split()),
}
_BENIGN_EXTENSION_ATTRIBUTES = frozenset({
    (_MC, "Ignorable"), (_W14, "paraId"), (_W14, "textId"),
    (_W15, "collapsed"), (_W16DU, "dateUtc"),
})
_KNOWN_EXTENSION_NAMES = {
    _THEME_2012: frozenset({"themeFamily"}),
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

# These are inspection grammars, not permission to mutate or remove metadata.
# See MS-DOCX 5.1/5.2/5.4/5.6 and the SDK fixed-part declarations. In particular,
# recognizing a prefix or an mc:Ignorable token never recognizes its namespace.
_LIGATURES = frozenset("""
none standard contextual historical discretional standardContextual
standardHistorical contextualHistorical standardDiscretional contextualDiscretional
historicalDiscretional standardContextualHistorical standardContextualDiscretional
standardHistoricalDiscretional contextualHistoricalDiscretional all
""".split())
_METADATA_RELATIONSHIPS = {
    "http://schemas.microsoft.com/office/2011/relationships/people": (
        frozenset({_WORD_CT_BASE + "people+xml"}), "{" + _W15 + "}people"),
    "http://schemas.microsoft.com/office/2011/relationships/commentsExtended": (
        frozenset({_WORD_CT_BASE + "commentsExtended+xml"}), "{" + _W15 + "}commentsEx"),
    "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds": (
        frozenset({_WORD_CT_BASE + "commentsIds+xml", "application/vnd.ms-word.commentsIds+xml"}),
        "{" + _W16CID + "}commentsIds"),
    "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible": (
        frozenset({_WORD_CT_BASE + "commentsExtensible+xml", "application/vnd.ms-word.commentsExtensible+xml"}),
        "{" + _W16CEX + "}commentsExtensible"),
}
_METADATA_ROOT_RELATIONSHIPS = {
    root: (kind, content_types) for kind, (content_types, root) in _METADATA_RELATIONSHIPS.items()
}
_SAFE_RELATIONSHIP_TYPES |= frozenset(_METADATA_RELATIONSHIPS)


def _white_content(node):
    return not (node.text or "").strip() and all(not (child.tail or "").strip() for child in node)


def _sequence(node, groups):
    """Check a closed sequence without accepting arbitrary schema extensions."""
    if not _white_content(node):
        return False
    children, index = list(node), 0
    for tag, minimum, maximum in groups:
        start = index
        while index < len(children) and children[index].tag == tag:
            index += 1
        if not minimum <= index - start <= maximum:
            return False
    return index == len(children)


def _root_attributes(node):
    if set(node.attrib) - {"{" + _MC + "}Ignorable"}:
        return False
    ignored = node.get("{" + _MC + "}Ignorable")
    return ignored is None or (bool(ignored.split()) and all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", prefix) and prefix in node.nsmap
        for prefix in ignored.split()))


def _leaf(node, required, optional=()):
    return (not len(node) and _white_content(node)
            and set(required) <= set(node.attrib) <= set(required) | set(optional))


def _hex8(value):
    return re.fullmatch(r"[0-9A-Fa-f]{8}", value or "") is not None


def _metadata_part(root, content_type):
    """Recognize a whole closed metadata tree, never a namespace in isolation.

    SharePoint FormTemplates has three text leaves (Microsoft's FormTemplates
    Schema Overview). Dynamic content-type XSDs, document-management properties,
    InfoPath partner controls and arbitrary custom fields remain unsupported.
    """
    if root.tag == "{" + _SP_FORMS + "}FormTemplates":
        return (content_type in {"application/xml", "text/xml"} and not root.attrib
                and _sequence(root, [("{" + _SP_FORMS + "}" + n, 1, 1) for n in ("Display", "Edit", "New")])
                and all(not n.attrib and not len(n) and 0 < len(n.text or "") <= 255
                        and (n.text or "").strip() == n.text for n in root))
    if root.tag == "{" + _W15 + "}people":
        person, presence = ("{" + _W15 + "}" + n for n in ("person", "presenceInfo"))
        if (content_type != _WORD_CT_BASE + "people+xml" or not _root_attributes(root)
                or not _sequence(root, [(person, 0, MAX_INVENTORY_XML_NODES)])):
            return False
        authors = []
        for node in root:
            author = node.get("{" + _W15 + "}author", "")
            if (set(node.attrib) != {"{" + _W15 + "}author"} or not 0 < len(author) <= 255
                    or not _sequence(node, [(presence, 0, 1)])):
                return False
            authors.append(author)
            for identity in node:
                required = {"{" + _W15 + "}" + n for n in ("providerId", "userId")}
                if (not _leaf(identity, required)
                        or any(not 0 < len(value) <= 20_000 for value in identity.attrib.values())):
                    return False
        return len(authors) == len(set(authors))
    comment_roots = {
        "{" + _W15 + "}commentsEx": (_W15, "commentEx", ("paraId",), ("paraIdParent", "done")),
        "{" + _W16CID + "}commentsIds": (_W16CID, "commentId", ("paraId", "durableId"), ()),
        "{" + _W16CEX + "}commentsExtensible": (_W16CEX, "commentExtensible", ("durableId",), ("dateUtc", "intelligentPlaceholder")),
    }
    rule = comment_roots.get(root.tag)
    if rule is None:
        return False
    if content_type not in _WORD_CONTENT_TYPES or _TYPED_XML_ROOTS.get(content_type) != root.tag:
        return False
    namespace, child, required, optional = rule
    def q(name):
        return "{" + namespace + "}" + name
    if not _root_attributes(root) or not _sequence(root, [(q(child), 0, MAX_INVENTORY_XML_NODES)]):
        return False
    identities = {key: set() for key in required}
    for node in root:
        if not _leaf(node, {q(n) for n in required}, {q(n) for n in optional}):
            return False
        for key, value in node.attrib.items():
            local = etree.QName(key).localname
            if local in {"paraId", "paraIdParent", "durableId"} and not _hex8(value):
                return False
            # CT_CommentId defines a positive signed-31-bit durable identity;
            # CT_CommentExtensible references that same identity.
            if local == "durableId" and not 0 < int(value, 16) < 0x7FFFFFFF:
                return False
            if local in {"done", "intelligentPlaceholder"} and value not in {"0", "1", "true", "false"}:
                return False
            if local == "dateUtc":
                try:
                    if (len(value) > 64 or re.fullmatch(
                            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-]00:00)", value) is None):
                        return False
                    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
                    if datetime.fromisoformat(normalized).utcoffset().total_seconds() != 0:
                        return False
                except (ValueError, AttributeError):
                    return False
        for key in required:
            value = node.get(q(key)).upper()
            if value in identities[key]:
                return False
            identities[key].add(value)
    return True


def _web_divs(root):
    """Closed CT_Divs/CT_Div/CT_DivBdr subset; numeric twips, no future values."""
    border_values = {"nil", "none", "single", "double", "dotted", "dashed", "dotDash", "dotDotDash"}
    border_attributes = {w(n) for n in ("val", "color", "themeColor", "themeTint", "themeShade", "sz", "space", "shadow", "frame")}
    themes = {"dark1", "light1", "dark2", "light2", "hyperlink", "followedHyperlink", "none",
              *("accent" + str(i) for i in range(1, 7))}
    seen = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag in {w("divs"), w("divsChild")}:
            if node.attrib or not _sequence(node, [(w("div"), 1, MAX_INVENTORY_XML_NODES)]):
                return False
        elif node.tag == w("div"):
            ident = node.get(w("id"), "")
            if (set(node.attrib) != {w("id")} or re.fullmatch(r"-?[1-9][0-9]{0,9}", ident) is None
                    or not -(2**31) <= int(ident) <= 2**31 - 1 or ident in seen
                    or not _sequence(node, [(w(n), low, high) for n, low, high in (
                        ("blockQuote", 0, 1), ("bodyDiv", 0, 1), ("marLeft", 1, 1), ("marRight", 1, 1),
                        ("marTop", 1, 1), ("marBottom", 1, 1), ("divBdr", 0, 1), ("divsChild", 0, MAX_INVENTORY_XML_NODES))])):
                return False
            seen.add(ident)
        elif node.tag == w("divBdr"):
            if node.attrib or not _sequence(node, [(w(n), 0, 1) for n in ("top", "left", "bottom", "right")]):
                return False
        elif node.tag in {w(n) for n in ("blockQuote", "bodyDiv")}:
            if not _leaf(node, (), {w("val")}) or node.get(w("val"), "true") not in {"true", "false", "on", "off", "1", "0"}:
                return False
        elif node.tag in {w(n) for n in ("marLeft", "marRight", "marTop", "marBottom")}:
            value = node.get(w("val"), "")
            if (not _leaf(node, {w("val")}) or re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})", value) is None
                    or not -(2**31) <= int(value) <= 2**31 - 1):
                return False
        elif node.tag in {w(n) for n in ("top", "left", "bottom", "right")}:
            if not _leaf(node, {w("val")}, border_attributes) or node.get(w("val")) not in border_values:
                return False
            for key, value in node.attrib.items():
                local = etree.QName(key).localname
                if local == "color" and value != "auto" and re.fullmatch(r"[0-9A-Fa-f]{6}", value) is None:
                    return False
                if local == "themeColor" and value not in themes:
                    return False
                if local in {"themeTint", "themeShade"} and re.fullmatch(r"[0-9A-Fa-f]{2}", value) is None:
                    return False
                if local in {"sz", "space"} and (len(value) > 3 or not value.isascii() or not value.isdigit()
                        or int(value) > (255 if local == "sz" else 31)):
                    return False
                if local in {"shadow", "frame"} and value not in {"true", "false", "on", "off", "1", "0"}:
                    return False
        else:
            return False
        stack.extend(node)
    return True


def _ligature_context(properties, root):
    """Close only the new property's ancestry, not the old WML vocabulary.

    MS-DOCX CT_Ligatures extends run properties. A familiar immediate owner
    does not make a misplaced style, numbering level or text run a valid
    context. This story profile covers current/original run properties and
    paragraph marks. Document defaults have a separate, bound preservation
    profile below; style/list/history defaults remain outside both profiles.
    """
    inline = {w(n) for n in ("ins", "del", "moveFrom", "moveTo", "hyperlink", "fldSimple", "smartTag")}
    stories = {w(n) for n in ("body", "tc", "hdr", "ftr", "comment", "footnote", "endnote")}
    parents = {
        w("rPr"): {w("r"), w("pPr"), w("rPrChange")},
        w("r"): {w("p")} | inline,
        **{tag: {w("p")} | inline for tag in inline},
        # Only CT_PPr (under p) owns paragraph-mark run properties. The
        # similarly named style/default/level/history pPr types do not.
        w("pPr"): {w("p")}, w("rPrChange"): {w("rPr")},
        w("p"): stories, w("tbl"): stories,
        w("tc"): {w("tr")}, w("tr"): {w("tbl")},
        w("body"): {w("document")},
        w("comment"): {w("comments")}, w("footnote"): {w("footnotes")}, w("endnote"): {w("endnotes")},
    }
    current, paragraphs, histories = properties, 0, 0
    while current is not root:
        paragraphs += current.tag == w("p")
        histories += current.tag == w("rPrChange")
        if paragraphs > 1 or histories > 1:
            return False
        parent = current.getparent()
        if parent is None or parent.tag not in parents.get(current.tag, ()):
            return False
        current = parent
    return paragraphs == 1


def _default_ligature(root, name, content_type, roots):
    """Exact Word-saved default leaf; no new style/default property vocabulary.

    Ecma-376 Part 4 CT_RPrDefault/rPr uses CT_RPr (Part 1 17.7.5.4).
    MS-DOCX 2.2.1 extends CT_RPr, but names the ordinary-run clause: using
    this one leaf in defaults is a bounded type-based preservation inference.
    MS-OI29500 2.1.245 rules out broad CT_RPr equivalence in Word defaults.
    """
    if (name != "word/styles.xml" or root.tag != w("styles")
            or content_type != _WORD_CT_BASE + "styles+xml"):
        return None
    prefixes = root.get("{" + _MC + "}Ignorable", "").split()
    if not any(root.nsmap.get(prefix) == _W14 for prefix in prefixes):
        return None
    current = root
    for tag in ("docDefaults", "rPrDefault", "rPr"):
        found = current.findall(w(tag))
        if len(found) != 1:
            return None
        current = found[0]
        if current.attrib or (current.text or "").strip() or (current.tail or "").strip():
            return None
    if any(n.tag in {w(tag) for tag in ("cs", "highlight", "oMath", "rPrChange", "rStyle", "rtl")}
           for n in current):
        return None
    leaves = current.findall("{" + _W14 + "}ligatures")
    if (any((child.tail or "").strip() for child in current)
            or len(leaves) != 1 or leaves[0].getnext() is not None
            or not _leaf(leaves[0], {"{" + _W14 + "}val"})
            or leaves[0].get("{" + _W14 + "}val") not in _LIGATURES):
        return None
    links = [(part, node) for part, rels in roots.items()
             if rels.tag == "{" + _REL + "}Relationships"
             for node in rels if node.get("Type") == _ODREL + "styles"]
    if (len(links) != 1 or links[0][0] != "word/_rels/document.xml.rels"
            or links[0][1].get("Target") not in {"styles.xml", "/word/styles.xml"}
            or links[0][1].get("TargetMode", "Internal") != "Internal"):
        return None
    return leaves[0]


def _word_metadata(node, root, content_type, web_cache, locked_cache, default_ligature=None):
    """Return None for old vocabulary; new vocabulary needs exact context."""
    parent = node.getparent()
    if node.tag == w("locked"):
        if not (root.tag == w("styles") and content_type in {_WORD_CT_BASE + "styles+xml", "application/vnd.ms-word.stylesWithEffects+xml"}
                and parent is not None and parent.tag == w("style") and parent.getparent() is root):
            return False
        if parent not in locked_cache:
            locked_cache[parent] = sum(child.tag == w("locked") for child in parent)
        return (locked_cache[parent] == 1
                and _leaf(node, (), {w("val")}) and node.get(w("val"), "on") in {"on", "off"})
    if node.tag in {w("divBdr"), w("divsChild"), w("divs")}:
        current = node
        while current.getparent() is not None and current.getparent().tag != w("webSettings"):
            current = current.getparent()
        if current not in web_cache:
            web_cache[current] = (root.tag == w("webSettings") and content_type == _WORD_CT_BASE + "webSettings+xml"
                and current.tag == w("divs") and current.getparent() is root and _web_divs(current))
        return web_cache[current]
    if node.tag == "{" + _W14 + "}ligatures":
        if node is default_ligature:
            return True
        following = node.getnext()
        original = parent is not None and parent.getparent() is not None and parent.getparent().tag == w("rPrChange")
        return (root.tag in {w(n) for n in ("document", "hdr", "ftr", "comments", "footnotes", "endnotes")}
                and content_type in _WORD_CONTENT_TYPES and parent is not None and parent.tag == w("rPr")
                and (following is None or (not original and following.tag == w("rPrChange")
                                           and following.getnext() is None))
                and _ligature_context(parent, root)
                and _leaf(node, {"{" + _W14 + "}val"}) and node.get("{" + _W14 + "}val") in _LIGATURES)
    return None


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


_USER_DATA_CT = "application/xml"
_USER_PROPS_CT = "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"
_USER_DATA_REL = _ODREL + "customXml"
_USER_PROPS_REL = _ODREL + "customXmlProps"
_XSD = "http://www.w3.org/2001/XMLSchema"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_XSLT = "http://www.w3.org/1999/XSL/Transform"
_ASCII_PART_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_USER_CONSUMER_WORD_NAMES = frozenset("""
fldSimple fldChar fldData instrText delInstrText dataBinding
sdt sdtPr sdtContent sdtEndPr contentPart drawing pict object altChunk subDoc
attachedSchema schemaLibrary attachedTemplate mailMerge saveThroughXslt
useXSLTWhenSaving saveXmlDataOnly
""".split())


def _xml_like(name, content_type, data):
    return (name == "[Content_Types].xml" or name.lower().endswith((".xml", ".rels"))
            or bool(content_type and (content_type.endswith("+xml")
                                     or content_type in {"application/xml", "text/xml"}))
            or data.lstrip().startswith(b"<"))


def _reserved_storage_namespace(value):
    """Office roles are not arbitrary user data, including declared-only URIs."""
    return value.lower().startswith((
        "http://schemas.microsoft.com/", "https://schemas.microsoft.com/",
        "urn:schemas-microsoft-com:", "http://schemas.openxmlformats.org/",
        "https://schemas.openxmlformats.org/", "http://purl.oclc.org/ooxml/",
        "https://purl.oclc.org/ooxml/",
    ))


def _user_payload(root):
    # Arbitrary XML is retained, not schema-validated or executed. Keep Office
    # vocabulary and external-schema/XSLT mechanisms out of this first profile.
    for event, node in etree.iterwalk(root, events=("start", "start-ns")):
        if event == "start-ns":
            uri = node[1] or ""
            if _reserved_storage_namespace(uri) or uri == _XSLT:
                return False
            continue
        qname = etree.QName(node)
        if (_reserved_storage_namespace(qname.namespace or "")
                or qname.namespace == _XSLT
                or node.tag in {"{" + _XSD + "}" + n for n in ("import", "include", "redefine", "override")}):
            return False
        for key, value in node.attrib.items():
            attr = etree.QName(key)
            if (_reserved_storage_namespace(attr.namespace or "")
                    or _reserved_storage_namespace(value.strip())
                    or attr.namespace == _XSLT
                    or attr.namespace == _XSI and attr.localname in {"schemaLocation", "noNamespaceSchemaLocation"}):
                return False
    return True


def _user_properties(root, payload):
    item, refs, ref = ("{" + _CUSTOM_XML + "}" + n for n in ("datastoreItem", "schemaRefs", "schemaRef"))
    # An explicit list is required. Empty means no associated schemas; the
    # narrow named form retains a validation association, not an assertion that
    # no schema will be used. Neither form links body text to the stored data.
    if (root.tag != item or set(root.attrib) != {"{" + _CUSTOM_XML + "}itemID"}
            or not _sequence(root, [(refs, 1, 1)])
            or root[0].attrib or not _sequence(root[0], [(ref, 0, 1)])
            or re.fullmatch(r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}",
                            root.get("{" + _CUSTOM_XML + "}itemID", "")) is None):
        return False
    if not len(root[0]):
        return True
    reference = root[0][0]
    uri = reference.get("{" + _CUSTOM_XML + "}uri", "")
    return (_leaf(reference, {"{" + _CUSTOM_XML + "}uri"})
            and uri == etree.QName(payload).namespace
            and re.fullmatch(r"(?:urn:[A-Za-z0-9][A-Za-z0-9-]{1,31}:[A-Za-z0-9._~:-]+"
                             r"|https?://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?:/[A-Za-z0-9._~/-]*)?)", uri) is not None
            and not _reserved_storage_namespace(uri) and uri != _XSLT)


def _storage_root_ignorable(root, content_type):
    """Allow one root annotation, without treating unknown content as ignored.

    Every used element/attribute must still pass the existing whole-part
    inventory. This does not implement MC preprocessing or alternate content.
    Non-Word parts and user payloads cannot acquire this exception.
    """
    if content_type not in _WORD_CONTENT_TYPES or root.tag != _TYPED_XML_ROOTS.get(content_type):
        return False
    tokens = root.get("{" + _MC + "}Ignorable", "").split()
    namespaces = root.nsmap
    return (bool(tokens) and len(tokens) == len(set(tokens))
            and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", prefix) is not None
                    and prefix in namespaces and namespaces[prefix]
                    and namespaces[prefix] not in {W_NS, _MC, _XML, _REL, _CT, _ODREL.rstrip("/")}
                    for prefix in tokens))


def _user_storage_roles(parts, types, roots):
    """Propose roles only after a complete strict OPC/negative-consumer scan.

    No filename, namespace or absent known-consumer hit is authority by itself.
    The caller must additionally establish complete existing nonpayload coverage
    before publishing any role label. Old closed metadata profiles stay separate.
    """
    graph_ok, edges = True, []
    types_root = roots["[Content_Types].xml"]
    if types_root.attrib or not _white_content(types_root):
        graph_ok = False
    for node in types_root:
        allowed = ({"Extension", "ContentType"} if node.tag == "{" + _CT + "}Default"
                   else {"PartName", "ContentType"} if node.tag == "{" + _CT + "}Override" else None)
        if allowed is None or not _leaf(node, allowed):
            graph_ok = False
    for name, root in roots.items():
        is_rels = (name.lower().endswith(".rels") or types[name] == "application/vnd.openxmlformats-package.relationships+xml"
                   or root.tag == "{" + _REL + "}Relationships")
        if not is_rels:
            continue
        if name == "_rels/.rels":
            owner = None
        else:
            folder, leaf = posixpath.split(name)
            owner = posixpath.join(posixpath.dirname(folder), leaf[:-5])
            if (posixpath.basename(folder) != "_rels" or not leaf.endswith(".rels")
                    or owner not in parts):
                graph_ok = False
        if (types[name] != "application/vnd.openxmlformats-package.relationships+xml"
                or root.tag != "{" + _REL + "}Relationships" or root.attrib
                or not _white_content(root)):
            graph_ok = False
        seen = set()
        for index, node in enumerate(root):
            errors = []
            target = _relationship(node, name, [index], parts, lambda *args: errors.append(args))
            ident, raw = node.get("Id"), node.get("Target", "")
            if (errors or not _leaf(node, {"Id", "Type", "Target"}, {"TargetMode"})
                    or not ident or ident in seen or "%" in raw
                    or posixpath.normpath(raw) != raw):
                graph_ok = False
            seen.add(ident)
            edges.append((owner, node.get("Type"), target, node.get("TargetMode", "Internal")))
    # Do not promote the existing bibliography/FormTemplates compatibility paths
    # into generic opacity, or change those paths when no new user store exists.
    legacy_roots = {"{" + _BIBLIOGRAPHY + "}Sources", "{" + _SP_FORMS + "}FormTemplates"}
    attempted = {target for _, kind, target, _ in edges if kind == _USER_DATA_REL
                 and target in roots and roots[target].tag not in legacy_roots}
    if not attempted:
        return {}, set()
    incoming, outgoing = {}, {}
    for edge in edges:
        outgoing.setdefault(edge[0], []).append(edge)
        incoming.setdefault(edge[2], []).append(edge)
    main_edges = [edge for edge in edges if edge[1] == _ODREL + "officeDocument"]
    if (main_edges != [(None, _ODREL + "officeDocument", DOCUMENT_PART, "Internal")]
            or types.get(DOCUMENT_PART) != _WORD_CT_BASE + "document.main+xml"
            or roots.get(DOCUMENT_PART) is None or roots[DOCUMENT_PART].tag != w("document")):
        graph_ok = False
    # All stored r:* and MC constructs remain excluded except the closed root
    # annotation. An Ignorable prefix never grants permission to skip content.
    for name, root in roots.items():
        root_ignorable = _storage_root_ignorable(root, types[name])
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            qname = etree.QName(node)
            if (qname.namespace == _MC or qname.localname == "contentPart"
                    or qname.namespace == W_NS and (qname.localname in _USER_CONSUMER_WORD_NAMES
                                                    or qname.localname.startswith("customXml"))
                    or any(etree.QName(key).namespace == _ODREL.rstrip("/")
                           or etree.QName(key).namespace == _MC
                           and not (node is root and key == "{" + _MC + "}Ignorable" and root_ignorable)
                           for key in node.attrib)):
                graph_ok = False
    props = {name for name, root in roots.items()
             if root.tag == "{" + _CUSTOM_XML + "}datastoreItem" or types[name] == _USER_PROPS_CT}
    ids = []
    for name, root in roots.items():
        for node in root.iter("{" + _CUSTOM_XML + "}datastoreItem"):
            ident = node.get("{" + _CUSTOM_XML + "}itemID", "")
            if (node is not root or name not in props
                    or re.fullmatch(r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}", ident) is None):
                graph_ok = False
            ids.append(ident.upper())
    if len(ids) != len(set(ids)):
        graph_ok = False
    proposed = {}
    for name in attempted:
        ins, outs = incoming.get(name, []), outgoing.get(name, [])
        if (ins != [(DOCUMENT_PART, _USER_DATA_REL, name, "Internal")]
                or types.get(name) != _USER_DATA_CT or not _user_payload(roots[name])
                or len(outs) != 1 or outs[0][1] != _USER_PROPS_REL
                or outs[0][3] != "Internal"):
            graph_ok = False
            continue
        prop = outs[0][2]
        if (prop not in props or types.get(prop) != _USER_PROPS_CT
                or not _user_properties(roots[prop], roots[name])
                or incoming.get(prop, []) != [(name, _USER_PROPS_REL, prop, "Internal")]
                or outgoing.get(prop)):
            graph_ok = False
            continue
        proposed[name] = prop
    # Orphan/aliased properties cannot supply authority for a different item.
    if props - set(proposed.values()):
        graph_ok = False
    return (proposed, set()) if graph_ok and len(proposed) == len(attempted) else ({}, attempted)


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
    parsed_roots, declared_types, metadata_links, bound_metadata = {}, {}, [], set()
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
        if name in parsed_roots:
            return parsed_roots[name]
        xml_bytes += len(data)
        _limit("inventory_xml_bytes", MAX_INVENTORY_XML_BYTES, xml_bytes)
        root = parse_xml(data)
        parsed_roots[name] = root
        # Existing per-part preflight already bounds attributes/namespaces.
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            _limit("inventory_xml_depth", MAX_INVENTORY_DEPTH, depth)
            if not isinstance(node.tag, str):
                xml_nodes += 1
                continue
            parent_ns = node.getparent().nsmap if node.getparent() is not None else {}
            xml_nodes += (1 + len(node.attrib)
                          + sum(parent_ns.get(key) != value for key, value in node.nsmap.items()))
            _limit("inventory_xml_nodes", MAX_INVENTORY_XML_NODES, xml_nodes)
            stack.extend((child, depth + 1) for child in reversed(node))
        _limit("inventory_xml_nodes", MAX_INVENTORY_XML_NODES, xml_nodes)
        return root

    # OPC part identity is ASCII case-insensitive, unlike raw ZIP keys. This is
    # an export gate: refuse ambiguity without normalizing or repairing names.
    member_identities = set()
    for name in sorted(package.parts):
        identity = name.translate(_ASCII_PART_CASE)
        if identity in member_identities:
            block("ambiguous_opc_part_identity", name)
        member_identities.add(identity)
    types_root = parse("[Content_Types].xml", package.parts["[Content_Types].xml"])
    if types_root.tag != f"{{{_CT}}}Types":
        raise DocxError("invalid OPC content-types root")
    defaults, overrides, override_identities = {}, {}, set()
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
            identity = key.translate(_ASCII_PART_CASE)
            if identity in override_identities:
                block("ambiguous_opc_override_identity", "[Content_Types].xml", [index])
            override_identities.add(identity)
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
        declared_types[name] = content_type
        if _xml_like(name, content_type, data):
            parse(name, data)
    user_storage, rejected_storage = _user_storage_roles(package.parts, declared_types, parsed_roots)
    for name in sorted(rejected_storage):
        block("unsupported_user_storage", name, [])

    for name, data in sorted(package.parts.items()):
        content_type = ("application/vnd.openxmlformats-package.content-types+xml"
                        if name == "[Content_Types].xml" else
                        overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower())))
        row = {"part_name": name, "content_type": content_type, "inspection": "uninspected"}
        declared_types[name] = content_type
        parts.append(row)
        if content_type is None:
            block("missing_part_content_type", name)
        if not _xml_like(name, content_type, data):
            if _image_signature(content_type, data):
                row["inspection"] = "recognized_raster"
            else:
                block("opaque_part_uninspected", name)
            continue
        root = types_root if name == "[Content_Types].xml" else parse(name, data)
        row["inspection"] = "xml"
        # Root-only mutation proofs cannot account for document siblings, and
        # processing instructions may declare semantics outside our vocabulary.
        # Keep the shared XML reader compatible; refuse only this clean gate.
        if (root.getprevious() is not None or root.getnext() is not None
                or any(isinstance(node, etree._ProcessingInstruction) for node in root.iter())):
            block("unsupported_auxiliary_xml", name, [])
        user_payload = name in user_storage
        metadata_ok = _metadata_part(root, content_type)
        metadata_nodes = frozenset(root.iter()) if metadata_ok else frozenset()
        metadata_roots = {"{" + ns + "}" + local for ns, local in (
            (_W15, "people"), (_W15, "commentsEx"), (_W16CID, "commentsIds"),
            (_W16CEX, "commentsExtensible"), (_SP_FORMS, "FormTemplates"))}
        if root.tag in metadata_roots and not metadata_ok:
            block("unsupported_auxiliary_xml", name, [])
        if metadata_ok and (root.tag in {"{" + _W15 + "}people", "{" + _SP_FORMS + "}FormTemplates"}
                or content_type in {_WORD_CT_BASE + "commentsIds+xml", _WORD_CT_BASE + "commentsExtensible+xml"}):
            # Existing legacy comment MIME parts may remain orphaned inventory
            # metadata. New canonical aliases and people need a proven binding;
            # every explicit incoming edge is checked below in both directions.
            bound_metadata.add(name)
        if (content_type in {"application/xml", "text/xml"}
                and root.tag not in set(_TYPED_XML_ROOTS.values()) | {
                    "{" + _BIBLIOGRAPHY + "}Sources", "{" + _CT + "}Types",
                } and not metadata_ok and not user_payload):
            block("unsupported_auxiliary_xml", name, [])
        if (name.startswith("customXml/") and not name.endswith(".rels")
                and root.tag not in {"{" + _BIBLIOGRAPHY + "}Sources", "{" + _CUSTOM_XML + "}datastoreItem"}
                and not user_payload
                and not (metadata_ok and root.tag == "{" + _SP_FORMS + "}FormTemplates")):
            block("unsupported_auxiliary_xml", name, [])
        if root.tag == w("numbering"):
            # Word can repair interleaved abstract/instance definitions by
            # dropping an instance and joining two independent list counters.
            order = tuple(w(n) for n in ("numPicBullet", "abstractNum", "num", "numIdMacAtCleanup"))
            children = [n.tag for n in root if isinstance(n.tag, str)]
            if (any(tag not in order for tag in children)
                    or children != sorted(children, key=lambda tag: order.index(tag) if tag in order else -1)
                    or children.count(w("numIdMacAtCleanup")) > 1):
                block("invalid_numbering_order", name, [])
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
        web_cache, locked_cache = {}, {}
        default_ligature = _default_ligature(root, name, content_type, parsed_roots)
        while stack:
            node, path, in_body = stack.pop()
            _limit("inventory_xml_depth", MAX_INVENTORY_DEPTH, len(path))
            if not isinstance(node.tag, str):
                continue
            in_body = in_body or node is body
            qname = etree.QName(node)
            local, namespace = qname.localname, qname.namespace
            word_metadata = _word_metadata(node, root, content_type, web_cache, locked_cache, default_ligature)
            if word_metadata is False:
                block("unsupported_auxiliary_xml", name, path)
            if ((namespace in {_W16CID, _W16CEX}
                 or (namespace == _W15 and local in {"people", "person", "presenceInfo", "commentsEx", "commentEx"})
                 or namespace == _SP_FORMS) and node not in metadata_nodes):
                block("unsupported_auxiliary_xml", name, path)
            if local in _REVISION_NAMES:
                supported = (namespace == W_NS and local in _ACCEPTABLE_KINDS
                             and name == DOCUMENT_PART and in_body)
                revisions.append({"part_name": name, "element_path": path,
                                  "kind": local, "supported_location": supported})
                _limit("inventory_revisions", MAX_INVENTORY_REVISIONS, len(revisions))
                if not supported:
                    block("unsupported_revision", name, path)
            elif namespace == W_NS and local not in _W_NAMES and word_metadata is not True:
                block("unknown_wordprocessing_markup", name, path)
            if (namespace in _STANDARD_ELEMENT_NAMES and local not in _STANDARD_ELEMENT_NAMES[namespace]
                    and not user_payload):
                block("unknown_standard_xml_markup", name, path)
            if content_type == "application/vnd.openxmlformats-officedocument.theme+xml" and namespace not in {_DRAWING, _THEME_2012}:
                block("unsupported_auxiliary_xml", name, path)
            if node.tag == "{" + _DRAWING + "}extLst":
                if (node.attrib or (node.text or "").strip()
                        or any(child.tag != "{" + _DRAWING + "}ext" or (child.tail or "").strip() for child in node)):
                    block("unsupported_auxiliary_xml", name, path)
            if node.tag == "{" + _DRAWING + "}ext" and (
                node.get("uri") is not None
                or node.getparent() is not None and node.getparent().tag == "{" + _DRAWING + "}extLst"
            ):
                if (node.getparent() is None or node.getparent().tag != "{" + _DRAWING + "}extLst"
                        or node.attrib != {"uri": _THEME_URI} or len(node) != 1
                        or node[0].tag != "{" + _THEME_2012 + "}themeFamily"
                        or (node.text or "").strip() or (node[0].tail or "").strip()):
                    block("unsupported_auxiliary_xml", name, path)
            if namespace == _THEME_2012:
                parent = node.getparent()
                if (local != "themeFamily" or len(node) or (node.text or "").strip()
                        or set(node.attrib) != {"name", "id", "vid"}
                        or not 0 < len(node.get("name", "")) <= 255
                        or any(re.fullmatch(r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}", node.get(key, "")) is None
                               for key in ("id", "vid"))
                        or parent is None
                        or parent.tag != "{http://schemas.openxmlformats.org/drawingml/2006/main}ext"
                        or parent.attrib != {"uri": _THEME_URI}):
                    block("unsupported_auxiliary_xml", name, path)
            if node.tag in {w("delText"), w("delInstrText")} and not any(
                ancestor.tag in {w("del"), w("moveFrom"), w("conflictDel")}
                for ancestor in node.iterancestors()
            ):
                block("stray_revision_atom", name, path)
            if (namespace not in _STANDARD_NAMESPACES
                    and local not in _KNOWN_EXTENSION_NAMES.get(namespace, ())
                    and node not in metadata_nodes and word_metadata is not True and not user_payload):
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
                        and (attr.namespace, attr.localname) not in _BENIGN_EXTENSION_ATTRIBUTES
                        and node not in metadata_nodes and not user_payload):
                    block("unknown_attribute_namespace", name, path)
            if root.tag == f"{{{_REL}}}Relationships" and node is not root:
                target = _relationship(node, name, path, package.parts, block)
                if target is not None:
                    metadata_links.append((name, path, node.get("Type"), target))
            for index in range(len(node) - 1, -1, -1):
                stack.append((node[index], [*path, index], in_body))
    validated_bindings, metadata_kinds = set(), set()
    for relationship_part, path, kind, target in metadata_links:
        rule = _METADATA_RELATIONSHIPS.get(kind)
        root = parsed_roots.get(target)
        target_rule = _METADATA_ROOT_RELATIONSHIPS.get(root.tag) if root is not None else None
        if rule is not None or target_rule is not None:
            expected_kind, allowed_types = target_rule if target_rule is not None else (None, ())
            if (relationship_part != "word/_rels/document.xml.rels"
                    or kind != expected_kind
                    or declared_types.get(target) not in allowed_types
                    or kind in metadata_kinds or target in validated_bindings):
                block("unsupported_relationship", relationship_part, path)
            else:
                validated_bindings.add(target)
                metadata_kinds.add(kind)
        elif root is not None and root.tag == "{" + _SP_FORMS + "}FormTemplates":
            if (relationship_part == "word/_rels/document.xml.rels" and kind == _ODREL + "customXml"
                    and target not in validated_bindings):
                validated_bindings.add(target)
            else:
                block("unsupported_relationship", relationship_part, path)
    for name in bound_metadata - validated_bindings:
        block("unsupported_auxiliary_xml", name, [])
    if DOCUMENT_PART not in package.parts:
        block("missing_main_document", DOCUMENT_PART)
    if "_rels/.rels" not in package.parts:
        block("missing_package_relationships", "_rels/.rels")
    if user_storage:
        if blockers:
            # An unknown outside context may hide a consumer. Never report a
            # complete store proof merely because the known-QName scan was empty.
            for name in sorted(user_storage):
                block("unproved_user_storage_context", name, [])
        else:
            for row in parts:
                if row["part_name"] in user_storage:
                    row["inspection"] = "opaque_user_storage"
                    row["preservation"] = "exact_bytes_only"
                    row["properties_part"] = user_storage[row["part_name"]]
    incomplete = {"opaque_part_uninspected", "unknown_xml_namespace", "unknown_standard_xml_markup",
                  "unknown_wordprocessing_markup", "unsupported_xml_content_type",
                  "missing_part_content_type", "unknown_attribute_namespace",
                  "opaque_content_reference", "unsupported_relationship",
                  "unsupported_external_relationship", "missing_relationship_target",
                  "unsupported_content_types_markup", "missing_declared_part",
                  "invalid_document_body", "missing_main_document"}
    incomplete |= {"invalid_numbering_order", "unsupported_main_content_type", "missing_package_relationships",
                   "unsupported_main_relationship", "part_content_type_mismatch"}
    incomplete |= {"unsupported_auxiliary_xml", "unsupported_user_storage", "unproved_user_storage_context",
                   "ambiguous_opc_part_identity", "ambiguous_opc_override_identity"}
    result = {"schema_version": "package_revisions.v1",
              "source_sha256": hashlib.sha256(payload).hexdigest(),
              "parts": parts, "revisions": revisions, "blockers": blockers,
              "complete": not any(item["code"] in incomplete for item in blockers),
              "clean_eligible": not blockers}
    # Advisory schema/field mapping is not an export proof. It must never clear
    # blockers, reclassify reserved stores or weaken the existing coverage gate.
    model = sharepoint_schema_model(parsed_roots)
    if model is not None:
        result["sharepoint_schema_model"] = model
    return result


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
        return None
    return resolved
