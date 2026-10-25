# SPDX-License-Identifier: Apache-2.0
"""Bounded reference scalar checks, not validation of embedded XSD or CAML.

Profile matches are conditional on a named reference type, not proof that an
import resolves to that type. Never compile/fetch schemas or authorize export.
"""
from __future__ import annotations

import datetime
import re

from lxml import etree

XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
DMS = "http://schemas.microsoft.com/office/2006/documentManagement/types"
META = "http://schemas.microsoft.com/office/2006/metadata/properties/metaAttributes"
MAX_VALUE_CHARS = 4096
MAX_FACETS = 64
XML_SPACE = " \t\r\n"
_DECIMAL = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})")
_XSD_TYPES = {"string": "string", "boolean": "boolean", "positiveInteger": "positive", "decimal": "decimal", "double": "double", "dateTime": "date"}
_DMS_TYPES = {"Text": "nonblank", "Note": "nonblank", "Unknown": "string", "Lookup": "positive", "UserId": "positive", "Boolean": "boolean", "Currency": "decimal", "Number": "double", "DateTime": "date_minimum"}


def _q(namespace, local):
    return "{" + namespace + "}" + local


def _attributes(node, allowed):
    return all(a in allowed or a.startswith("{" + META + "}") for a in node.attrib)


def _children(node):
    # Annotations are outside this scalar check; their semantics remain unproved.
    return [c for c in node if c.tag != _q(XSD, "annotation")]


def _reference(node, attribute, references):
    values = [r["qname"] for r in references.get(node, ()) if r["attribute"] == attribute]
    return values[0] if len(values) == 1 else None


def _profile(declaration, references, schema_namespaces, schema_imports, charge):
    """Recognize one closed direct type or anonymous scalar restriction."""
    charge(len(declaration.attrib) + len(declaration) + 1)
    if not _attributes(declaration, {"name", "type", "nillable", "id"}):
        return None, "unsupported_declaration_attributes"
    if declaration.get("nillable", "false") not in {"true", "false", "1", "0"}:
        return None, "unsupported_nillable_declaration"
    owner = declaration.getparent()
    if owner is not None:
        charge(len(owner.attrib) + 1)
    if owner is None or owner.tag != _q(XSD, "schema") or not _attributes(owner, {"targetNamespace", "elementFormDefault", "attributeFormDefault", "id", "version"}):
        return None, "unsupported_schema_context"
    nodes = _children(declaration)
    facets = []
    if "type" in declaration.attrib:
        if nodes:
            return None, "conflicting_type_declaration"
        base = _reference(declaration, "type", references)
    else:
        if len(nodes) != 1 or nodes[0].tag != _q(XSD, "simpleType") or not _attributes(nodes[0], {"id"}):
            return None, "unsupported_type_declaration"
        charge(len(nodes[0].attrib) + len(nodes[0]) + 1)
        nodes = _children(nodes[0])
        if len(nodes) != 1 or nodes[0].tag != _q(XSD, "restriction") or not _attributes(nodes[0], {"base", "id"}):
            return None, "unsupported_type_derivation"
        charge(len(nodes[0].attrib) + len(nodes[0]) + 1)
        base = _reference(nodes[0], "base", references)
        facets = _children(nodes[0])
    known = {_q(XSD, n): k for n, k in _XSD_TYPES.items()}
    known.update({_q(DMS, n): k for n, k in _DMS_TYPES.items()})
    kind = known.get(base)
    if kind is None:
        return None, "unrecognized_reference_type"
    namespace = XSD if base.startswith("{" + XSD + "}") else DMS
    if namespace in schema_namespaces:
        return None, "embedded_reference_type_definition"
    if namespace == DMS:
        imports = schema_imports.get(owner, {}).get(DMS, ())
        if len(imports) != 1 or set(imports[0].attrib) - {"namespace", "id"}:
            return None, "unproved_reference_import"
    if len(facets) > MAX_FACETS:
        return None, "facet_count_bound"
    maximum, choices = None, []
    for facet in facets:
        charge(len(facet.attrib) + len(facet) + 1)
        if kind not in {"string", "nonblank"} or not _attributes(facet, {"value", "id"}) or _children(facet):
            return None, "unsupported_scalar_facet"
        raw = facet.get("value")
        if raw is None or len(raw) > MAX_VALUE_CHARS:
            return None, "facet_value_bound"
        charge(len(raw))
        if facet.tag == _q(XSD, "maxLength"):
            if maximum is not None or not re.fullmatch(r"[0-9]{1,9}", raw):
                return None, "unsupported_length_facet"
            maximum = int(raw)
        elif facet.tag == _q(XSD, "enumeration"):
            if kind == "nonblank" and not raw.strip(XML_SPACE):
                return None, "inconsistent_enumeration_facet"
            choices.append(raw)
        else:
            return None, "unsupported_scalar_facet"
    if maximum is not None and any(len(v) > maximum for v in choices):
        return None, "inconsistent_enumeration_facet"
    return (base, kind, maximum, choices), None


def _date(value, minimum):
    """Only four-digit years with explicit timezone and <=6 fractional digits.

    Extended years, absent timezones and 24:00 remain outside this profile.
    Ordinal arithmetic avoids overflow at the Python date range boundaries.
    """
    match = _DATE.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, zone = match.groups()[6:]
    if hour == 24 or fraction is not None and len(fraction) > 6:
        return None
    try:
        date = datetime.date(year, month, day)
    except ValueError:
        return False
    if hour > 23 or minute > 59 or second > 59:
        return False
    offset = 0
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 14 or minutes > 59 or hours == 14 and minutes:
            return False
        offset = (hours * 60 + minutes) * 60 * (1 if zone[0] == "+" else -1)
    return not minimum or date.toordinal() * 86400 + hour * 3600 + minute * 60 + second - offset >= datetime.date(1900, 1, 1).toordinal() * 86400


def _lexical(value, kind):
    if kind == "string":
        return True
    if kind == "nonblank":
        return bool(value.strip(XML_SPACE))
    value = value.strip(XML_SPACE)
    if kind == "boolean":
        return value in {"true", "false", "0", "1"}
    if kind == "positive":
        return bool(re.fullmatch(r"\+?[0-9]+", value)) and any(c in "123456789" for c in value)
    if kind == "decimal":
        return bool(re.fullmatch(_DECIMAL, value))
    if kind == "double":
        return value in {"INF", "-INF", "NaN"} or bool(re.fullmatch(_DECIMAL + r"(?:[eE][+-]?[0-9]+)?", value))
    return _date(value, kind == "date_minimum")


def scalar_candidate_check(node, declaration, references, schema_namespaces, schema_imports, profiles, charge):
    """Return redacted, conditional reference-profile facts; never type proof."""
    result = {"profile_version": "sharepoint_scalar_candidate.v1", "status": "unsupported",
              "lexical_match": None, "type_definition_proven": False,
              "full_type_validity_proven": False, "business_semantics_proven": False}
    if declaration not in profiles:
        profiles[declaration] = _profile(declaration, references, schema_namespaces, schema_imports, charge)
    profile, reason = profiles[declaration]
    if profile is None:
        return {**result, "reason_code": reason}
    base, kind, maximum, choices = profile
    result["reference_type_qname"] = base
    if set(node.attrib) - {_q(XSI, "nil")}:
        return {**result, "reason_code": "instance_attributes_or_type_override"}
    nil = node.get(_q(XSI, "nil"), "false").strip(XML_SPACE)
    if nil not in {"true", "false", "1", "0"}:
        return {**result, "status": "invalid_value", "reason_code": "invalid_nil_flag"}
    # No descendant string coercion: structured properties and PIs are unproved.
    charge(len(node) + 1)
    if any(isinstance(c.tag, str) or not isinstance(c, etree._Comment) for c in node):
        return {**result, "reason_code": "structured_or_instruction_content"}
    pieces = [node.text or "", *(c.tail or "" for c in node)]
    length = sum(map(len, pieces))
    if length > MAX_VALUE_CHARS:
        return {**result, "reason_code": "value_length_bound"}
    charge(length + len(node) + 1)
    value = "".join(pieces)
    if nil in {"true", "1"}:
        valid = declaration.get("nillable") in {"true", "1"} and not value
        return {**result, "status": "consistent_nil_shape" if valid else "invalid_value",
                "reason_code": "nil_has_no_scalar_value" if valid else "inconsistent_explicit_nil"}
    match = _lexical(value, kind)
    if match is None:
        return {**result, "reason_code": "outside_date_profile"}
    if match and (maximum is not None and len(value) > maximum or choices and value not in choices):
        return {**result, "status": "invalid_value", "lexical_match": True, "reason_code": "string_facet_mismatch"}
    return {**result, "status": "matches_reference_profile" if match else "invalid_value",
            "lexical_match": match, "reason_code": "reference_scalar_and_facets_match" if match else "reference_lexical_mismatch"}
