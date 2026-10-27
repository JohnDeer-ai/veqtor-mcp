# SPDX-License-Identifier: Apache-2.0
"""Explicit, in-memory cleanup of an outgoing copy; never a working-file edit."""

import re
from copy import deepcopy

from lxml import etree

from . import _drafting_comments as c
from ._ooxml import load_validated_docx, parse_xml, w
from .apply import ApplyError, _output_archive_bytes
from .drafting import _require_unprotected
from .revision_inventory import inventory_package_revisions

CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCT = "http://purl.org/dc/terms/"
APP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
PROPS = {
    "core": (
        "docProps/core.xml",
        f"{{{CORE}}}coreProperties",
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
    ),
    "app": (
        "docProps/app.xml",
        f"{{{APP}}}Properties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
    ),
    "custom": (
        "docProps/custom.xml",
        f"{{{CUSTOM}}}Properties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties",
    ),
}
CORE_NAMES = {
    **{
        n: DC
        for n in (
            "title",
            "subject",
            "creator",
            "description",
            "identifier",
            "language",
        )
    },
    **{
        n: CORE
        for n in (
            "keywords",
            "lastModifiedBy",
            "revision",
            "lastPrinted",
            "category",
            "contentStatus",
            "version",
        )
    },
    **{n: DCT for n in ("created", "modified")},
}
APP_NAMES = {"Company", "Manager", "Application", "AppVersion", "TotalTime"}

SCALARS = {
    "lpstr",
    "lpwstr",
    "bstr",
    "i1",
    "i2",
    "i4",
    "i8",
    "int",
    "ui1",
    "ui2",
    "ui4",
    "ui8",
    "uint",
    "r4",
    "r8",
    "decimal",
    "date",
    "filetime",
    "bool",
    "error",
    "cy",
}


def _fail(code, detail):
    raise ApplyError(code, detail)


def policies(comments="preserve_all", properties="preserve_all"):
    """Closed, bounded policies; no implicit marker, author or resolved filter."""
    if comments != "preserve_all":
        if type(comments) is not dict:
            _fail("invalid_cleanup_policy", "choose an explicit comment policy")
        if comments == {"mode": "remove_all"}:
            pass
        elif (
            set(comments) == {"mode", "threads"}
            and comments["mode"] == "remove_threads"
        ):
            rows = comments["threads"]
            if type(rows) is not list or not 1 <= len(rows) <= 100:
                _fail("invalid_cleanup_policy", "select at most 100 complete threads")
            roots, members = set(), set()
            for row in rows:
                if type(row) is not dict or set(row) != {"root_id", "member_ids"}:
                    _fail(
                        "invalid_cleanup_policy",
                        "each thread requires its root and every message ID",
                    )
                ids = row["member_ids"]
                if (
                    type(ids) is not list
                    or not 1 <= len(ids) <= 100
                    or any(
                        type(i) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,9}", i)
                        for i in ids
                    )
                    or type(row["root_id"]) is not str
                    or row["root_id"] not in ids
                    or row["root_id"] in roots
                    or len(set(ids)) != len(ids)
                    or members.intersection(ids)
                ):
                    _fail(
                        "invalid_cleanup_policy",
                        "thread IDs must be unique and complete",
                    )
                roots.add(row["root_id"])
                members.update(ids)
            if len(members) > 100:
                _fail("resource_limit_exceeded", "select at most 100 comment messages")
        else:
            _fail("invalid_cleanup_policy", "unknown comment cleanup policy")
    if properties != "preserve_all":
        if (
            type(properties) is not dict
            or set(properties) != {"mode", "fields"}
            or properties["mode"] != "remove_selected"
            or type(properties["fields"]) is not list
            or not 1 <= len(properties["fields"]) <= 50
        ):
            _fail("invalid_cleanup_policy", "select at most 50 exact properties")
        seen = set()
        for row in properties["fields"]:
            if (
                type(row) is not dict
                or set(row) != {"part", "name"}
                or type(row["part"]) is not str
                or row["part"] not in PROPS
                or type(row["name"]) is not str
                or not 1 <= len(row["name"]) <= 255
                or any(ord(v) < 32 for v in row["name"])
                or (row["part"], row["name"]) in seen
            ):
                _fail(
                    "invalid_cleanup_policy",
                    "property identities must be exact and unique",
                )
            if (
                row["part"] == "core"
                and row["name"] not in CORE_NAMES
                or row["part"] == "app"
                and row["name"] not in APP_NAMES
            ):
                _fail("invalid_cleanup_policy", "unsupported standard property")
            seen.add((row["part"], row["name"]))
    return deepcopy(comments), deepcopy(properties)


def _comment_cleanup(parts, document, policy):
    comments = c._comment_root(parts)
    c._require_existing_binding(parts)
    metadata = c._thread_metadata(parts, comments)
    c._validate_thread_write_metadata(parts, metadata)
    # The export can remove other authors' messages, so require a closed
    # metadata profile instead of assuming unrecognized attributes are disposable.
    for kind, tree in metadata.trees.items():
        namespace, allowed = {
            "extended": (c._W15, {"paraId", "paraIdParent", "done"}),
            "ids": (c._W16CID, {"paraId", "durableId"}),
            "extensible": (
                c._W16CEX,
                {"durableId", "dateUtc", "intelligentPlaceholder"},
            ),
        }[kind]
        if set(tree.attrib) - {f"{{{c._MC}}}Ignorable"} or any(
            set(node.attrib) - {f"{{{namespace}}}{key}" for key in allowed}
            for node in tree
        ):
            _fail(
                "outgoing_cleanup_unsupported",
                "unknown comment metadata attributes must be preserved",
            )
    ids = {n.get(w("id")) for n in comments}
    # Whole-package coverage includes orphan anchors and renamed body collections.
    for name, root in [("word/document.xml", document), *c._deletion_xml_parts(parts)]:
        if any(n.tag == w("comments") for n in root.iter()) and name != c._COMMENTS:
            _fail(
                "outgoing_cleanup_unsupported",
                "nonstandard comment bodies require inspection",
            )
        for node in root.iter():
            if node.tag in c._ANCHOR_TAGS and node.get(w("id")) not in ids:
                _fail("outgoing_cleanup_unsupported", "orphan comment anchor")
    paragraphs = {
        c._hex_identity(p, f"{{{c._W14}}}paraId", optional=True)
        for n in comments
        for p in n.iter(w("p"))
    } - {None}
    durables = {
        c._hex_identity(n, f"{{{c._W16CID}}}durableId")
        for n in metadata.identities.values()
    }
    if (
        set(metadata.extended) - paragraphs
        or set(metadata.identities) - paragraphs
        or set(metadata.extensible) - durables
    ):
        _fail(
            "outgoing_cleanup_unsupported",
            "unbound comment metadata cannot be silently removed",
        )
    roots = {info["thread_root_id"] for info in metadata.summaries.values()}
    selected = set()
    if policy["mode"] == "remove_all":
        selected = ids
    else:
        for row in policy["threads"]:
            expected = {
                i
                for i, info in metadata.summaries.items()
                if info["thread_root_id"] == row["root_id"]
            }
            if row["root_id"] not in roots or expected != set(row["member_ids"]):
                _fail(
                    "comment_thread_selection_mismatch",
                    "select the whole current thread including all replies",
                )
            selected.update(expected)
    if any(
        set(node.attrib) - {w(key) for key in ("id", "author", "initials", "date")}
        for node in comments
        if node.get(w("id")) in selected
    ):
        _fail(
            "outgoing_cleanup_unsupported",
            "unknown selected comment attributes must be preserved",
        )
    # The shared deletion proof retains its ownership checks for all working-file
    # callers. Only this outgoing-copy path permits explicitly selected foreign messages.
    removed = (
        c._delete_selected_messages(
            parts, document, comments, metadata, selected, None, export_copy=True
        )
        if selected
        else []
    )
    if selected:
        parts["word/document.xml"] = c._xml(document)
    if policy["mode"] == "remove_all":
        remaining = c._comment_root(parts)
        after = c._thread_metadata(parts, remaining)
        if (
            len(remaining)
            or any(len(tree) for tree in after.trees.values())
            or any(
                n.tag in c._ANCHOR_TAGS
                for _, root in [
                    ("word/document.xml", document),
                    *c._deletion_xml_parts(parts),
                ]
                for n in root.iter()
            )
        ):
            _fail(
                "outgoing_cleanup_verification_failed",
                "comments remain after remove-all",
            )
    return removed


def _field_dependencies(parts):
    """Conservative independent-field profile; property-linked/unknown fields refuse.

    DATE/TIME and pagination can remain dynamic. No property is cleared while a
    DOCPROPERTY, AUTHOR, TITLE, nested/dynamic instruction or unknown field exists.
    """
    safe = {"DATE", "TIME", "PAGE", "NUMPAGES", "SECTION", "SECTIONPAGES"}
    roots = [
        ("word/document.xml", parse_xml(parts["word/document.xml"])),
        *c._deletion_xml_parts(parts),
    ]
    for _, root in roots:
        stack = []
        for node in root.iter():
            if node.tag == w("fldSimple"):
                instruction = node.get(w("instr"), "")
                if (
                    not instruction.split()
                    or instruction.split()[0].upper() not in safe
                ):
                    _fail(
                        "property_dependency_unverified",
                        "a field may consume document properties",
                    )
            elif node.tag == w("fldChar"):
                kind = node.get(w("fldCharType"))
                if kind == "begin" and not stack:
                    stack.append(["", False])
                elif kind == "separate" and stack and not stack[-1][1]:
                    stack[-1][1] = True
                elif kind == "end" and stack:
                    instruction, _ = stack.pop()
                    if (
                        not instruction.split()
                        or instruction.split()[0].upper() not in safe
                    ):
                        _fail(
                            "property_dependency_unverified",
                            "a field may consume document properties",
                        )
                else:
                    _fail(
                        "property_dependency_unverified",
                        "nested or malformed fields require inspection",
                    )
            elif node.tag == w("instrText"):
                if not stack or stack[-1][1]:
                    _fail("property_dependency_unverified", "unbound field instruction")
                stack[-1][0] += node.text or ""
            elif node.tag in {
                w("delInstrText"),
                w("dataBinding"),
                w("docPartObj"),
                w("docPartList"),
            }:
                _fail(
                    "property_dependency_unverified",
                    "property dependencies cannot be fully determined",
                )
        if stack:
            _fail("property_dependency_unverified", "incomplete field")


def _property_cleanup(parts, policy):
    _field_dependencies(parts)
    removed, trees = [], {}
    relationships = parse_xml(parts["_rels/.rels"])
    xml_roots = list(c._deletion_xml_parts(parts))
    for row in policy["fields"]:
        kind, name = row["part"], row["name"]
        path, tag, relationship = PROPS[kind]
        matches = [r for r in relationships if r.get("Type") == relationship]
        if (
            path not in parts
            or len(matches) != 1
            or matches[0].get("Target") not in {path, "/" + path}
            or matches[0].get("TargetMode", "Internal") != "Internal"
        ):
            _fail(
                "outgoing_cleanup_unsupported",
                "property part has no unambiguous canonical binding",
            )
        root = trees.setdefault(path, parse_xml(parts[path]))
        if root.tag != tag or any(
            other.tag == tag and other_path != path for other_path, other in xml_roots
        ):
            _fail("outgoing_cleanup_unsupported", "unexpected property part")
        if kind == "custom":
            names = [n.get("name", "").casefold() for n in root]
            raw_pids = [n.get("pid", "") for n in root]
            if any(
                not re.fullmatch(r"[0-9]{1,10}", pid)
                or not 2 <= int(pid) <= 2147483647
                for pid in raw_pids
            ):
                _fail(
                    "outgoing_cleanup_unsupported", "invalid custom property identity"
                )
            # PID is an integer identity: lexical aliases such as 2 and 02
            # cannot identify two distinct properties safely.
            pids = [int(pid) for pid in raw_pids]
            if len(names) != len(set(names)) or len(pids) != len(set(pids)):
                _fail(
                    "outgoing_cleanup_unsupported", "ambiguous custom property identity"
                )
        candidates = [
            n
            for n in root
            if (
                n.get("name") == name
                if kind == "custom"
                else n.tag == f"{{{CORE_NAMES[name] if kind == 'core' else APP}}}{name}"
            )
        ]
        if len(candidates) != 1:
            _fail(
                "property_selection_mismatch",
                "selected property must occur exactly once",
            )
        node = candidates[0]
        if kind == "custom":
            if (
                node.tag != f"{{{CUSTOM}}}property"
                or len(node) != 1
                or etree.QName(node[0]).namespace != VT
                or etree.QName(node[0]).localname not in SCALARS
                or len(node[0])
                or node[0].attrib
                or set(node.attrib) != {"fmtid", "pid", "name"}
                or node.get("fmtid", "").upper()
                != "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
                or not re.fullmatch(r"[0-9]{1,10}", node.get("pid", ""))
                or not 2 <= int(node.get("pid")) <= 2147483647
            ):
                _fail(
                    "outgoing_cleanup_unsupported",
                    "only ordinary scalar custom properties can be removed",
                )
            value, value_type = node[0].text or "", etree.QName(node[0]).localname
        else:
            allowed = (
                {"{http://www.w3.org/2001/XMLSchema-instance}type"}
                if kind == "core" and name in {"created", "modified"}
                else set()
            )
            if len(node) or set(node.attrib) - allowed:
                _fail(
                    "outgoing_cleanup_unsupported",
                    "only scalar standard properties can be removed",
                )
            value, value_type = node.text or "", "text"
        removed.append({**row, "value": value, "value_type": value_type})
        root.remove(node)
    for path, root in trees.items():
        parts[path] = c._xml(root)
    return removed


def prepare_cleanup(
    payload, *, comments_policy="preserve_all", properties_policy="preserve_all"
):
    comments_policy, properties_policy = policies(comments_policy, properties_policy)
    if comments_policy == properties_policy == "preserve_all":
        return payload, None
    inventory = inventory_package_revisions(payload)
    if not inventory["complete"] or any(
        r["inspection"] not in {"xml", "recognized_raster"} for r in inventory["parts"]
    ):
        _fail(
            "outgoing_cleanup_unsupported",
            "cleanup requires complete package coverage; preserve unknown data",
        )
    package = load_validated_docx(payload, capture=None)
    parts = dict(package.parts)
    _require_unprotected(parts)
    document = parse_xml(parts["word/document.xml"])
    # Dependencies are checked on the original package, before any selected
    # comment deletion could hide a property-consuming field.
    if properties_policy != "preserve_all":
        _field_dependencies(parts)
    removed_comments = (
        _comment_cleanup(parts, document, comments_policy)
        if comments_policy != "preserve_all"
        else []
    )
    removed_properties = (
        _property_cleanup(parts, properties_policy)
        if properties_policy != "preserve_all"
        else []
    )
    changed = sorted(name for name in parts if parts[name] != package.parts[name])
    candidate = _output_archive_bytes(package.infos, parts) if changed else payload
    checked = load_validated_docx(candidate, capture=None)
    if checked.parts != parts:
        _fail(
            "outgoing_cleanup_verification_failed",
            "serialized parts differ from the checked cleanup",
        )
    after = inventory_package_revisions(candidate)
    if not after["complete"]:
        _fail(
            "outgoing_cleanup_verification_failed",
            "cleanup introduced unverified package content",
        )
    return candidate, {
        "removed_comments": removed_comments,
        "removed_properties": removed_properties,
        "changed_parts": changed,
        "all_comments_removed": comments_policy == {"mode": "remove_all"},
        "confidentiality_clearance": "not_asserted",
    }
