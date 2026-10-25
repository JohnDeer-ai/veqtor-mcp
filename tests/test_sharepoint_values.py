# SPDX-License-Identifier: Apache-2.0
"""Literal scalar boundaries and refusal controls; no customer document data."""
import hashlib
import json
import random
import copy

import pytest
from lxml import etree

from veqtor_docx import clean_copy, revision_inventory
from veqtor_docx import _sharepoint_values as values
from test_sharepoint_metadata import DMS, F, Q, X, declaration, field, fixture, model
from test_revision_inventory import package


def scalar(kind, value, *, builtin=False):
    roots = fixture(value=value)
    schema = next(iter(roots.values()))
    restriction = next(declaration(roots).iter("{" + X + "}restriction"))
    if builtin:
        restriction.set("base", "x:" + kind)
    else:
        restriction.set("base", "d:" + kind)
    return roots, restriction, schema


def check(kind, value, **kwargs):
    roots, _, _ = scalar(kind, value, **kwargs)
    return field(model(roots))["value_check"]


@pytest.mark.parametrize("kind,value,expected", [
    ("Text", "", False), ("Text", " \t\r\n", False), ("Text", " A ", True),
    ("Text", "\u00a0", True), ("Text", "\u2009", True), ("Note", "", False),
    ("Note", "Fictional", True), ("Unknown", "", True), ("Unknown", " ", True),
    ("Lookup", "1", True), ("Lookup", "0", False), ("Lookup", "-1", False),
    ("Lookup", "+0007", True), ("Lookup", " \t7\r\n", True),
    ("Lookup", "\u00a07\u00a0", False), ("Lookup", "7.0", False),
    ("Lookup", "7e0", False), ("Lookup", "7_000", False), ("Lookup", "７", False),
    ("UserId", "3", True), ("UserId", "0", False), ("UserId", "-3", False),
    ("Boolean", "true", True), ("Boolean", "false", True), ("Boolean", "1", True),
    ("Boolean", "0", True), ("Boolean", " true ", True), ("Boolean", "TRUE", False),
    ("Boolean", "yes", False), ("Currency", "4.32", True), ("Currency", "-0", True),
    ("Currency", ".32", True), ("Currency", "4.", True), ("Currency", "4,32", False),
    ("Currency", "1e2", False), ("Currency", "NaN", False), ("Currency", "INF", False),
    ("Number", "1e2", True), ("Number", "NaN", True), ("Number", "INF", True),
    ("Number", "-INF", True), ("Number", "+INF", False), ("Number", "nan", False),
    ("Number", "1e", False), ("Number", "1e+", False), ("Number", "1e-", False),
    ("Number", "6E", False),
    ("Number", "1_000", False), ("Number", "1,000", False),
    ("DateTime", "2026-10-25T00:00:00Z", True), ("DateTime", "1900-01-01T00:00:00Z", True),
    ("DateTime", "1899-12-31T23:59:59Z", False),
    ("DateTime", "1900-01-01T00:00:00+01:00", False),
    ("DateTime", "1899-12-31T23:00:00-01:00", True),
    ("DateTime", "1900-01-01T00:00:00.000001Z", True),
    ("DateTime", "2000-02-29T12:00:00Z", True), ("DateTime", "2100-02-29T12:00:00Z", False),
    ("DateTime", "2026-13-25T00:00:00Z", False), ("DateTime", "2026-10-25T12:60:00Z", False),
    ("DateTime", "2026-10-25T00:00:00+14:01", False),
])
def test_literal_reference_profiles(kind, value, expected):
    result = check(kind, value)
    assert result["status"] == ("matches_reference_profile" if expected else "invalid_value")
    assert result["lexical_match"] is expected
    assert not result["type_definition_proven"]
    assert not result["full_type_validity_proven"]
    assert not result["business_semantics_proven"]


@pytest.mark.parametrize("kind,value,expected", [
    ("string", "", True), ("boolean", "TRUE", False), ("positiveInteger", "-0", False),
    ("decimal", "+1.5", True), ("double", "-1e+3", True),
    ("dateTime", "1899-12-31T23:59:59Z", True),
])
def test_builtin_reference_profiles(kind, value, expected):
    assert check(kind, value, builtin=True)["lexical_match"] is expected


@pytest.mark.parametrize("value", ["2026-10-25", "2026-10-25T00:00:00", "2026-10-25T24:00:00Z",
    "12026-10-25T00:00:00Z", "2026-10-25T00:00:00.0000001Z", "-0001-10-25T00:00:00Z"])
def test_dates_outside_finite_subset_are_unproved(value):
    result = check("DateTime", value)
    assert result["status"] == "unsupported"
    assert result["reason_code"] == "outside_date_profile"
    assert result["lexical_match"] is None


@pytest.mark.parametrize("kind,value,facet,facet_value,status", [
    ("Text", "abcdefgh", "maxLength", "8", "matches_reference_profile"),
    ("Text", "abcdefghi", "maxLength", "8", "invalid_value"),
    ("Text", "😀", "maxLength", "1", "matches_reference_profile"),
    ("Text", "Fictional A", "enumeration", "Fictional A", "matches_reference_profile"),
    ("Text", "Fictional B", "enumeration", "Fictional A", "invalid_value"),
    ("Text", "abc", "pattern", ".*", "unsupported"),
    ("Text", "abc", "whiteSpace", "collapse", "unsupported"),
    ("Number", "3", "maxLength", "3", "unsupported"),
    ("Text", "abc", "maxLength", "-1", "unsupported"),
])
def test_declared_facets_are_checked_or_explicitly_unproved(kind, value, facet, facet_value, status):
    roots, restriction, _ = scalar(kind, value)
    etree.SubElement(restriction, "{" + X + "}" + facet, value=facet_value)
    result = field(model(roots))["value_check"]
    assert result["status"] == status
    assert facet_value not in json.dumps(result) or facet_value in {"1", "3", "8"}


@pytest.mark.parametrize("mutation", ["no_import", "remote_import", "unknown_type", "named_type",
    "complex_type", "fixed", "default", "substitutionGroup", "type_and_inline", "two_restrictions",
    "embedded_vendor", "schema_default", "instance_type", "instance_attribute", "structured", "PI",
    "duplicate_maximum", "inconsistent_enumeration", "unknown_facet_attribute", "many_facets"])
def test_unsupported_semantics_never_become_a_profile_match(mutation):
    roots, restriction, schema = scalar("Text", "Fictional private scalar")
    decl = declaration(roots)
    props = list(roots.values())[1]
    leaf = props.find("documentManagement/" + Q)
    if mutation == "no_import":
        schema[1].remove(schema[1][0])
    elif mutation == "remote_import":
        schema[1][0].set("schemaLocation", "https://invalid.example/never-fetch.xsd")
    elif mutation in {"unknown_type", "named_type"}:
        restriction.set("base", "d:Future" if mutation == "unknown_type" else "f:Named")
    elif mutation == "complex_type":
        decl.remove(decl[0])
        etree.SubElement(decl, "{" + X + "}complexType")
    elif mutation in {"fixed", "default", "substitutionGroup"}:
        decl.set(mutation, "Fictional secret")
    elif mutation == "type_and_inline":
        decl.set("type", "d:Text")
    elif mutation == "two_restrictions":
        etree.SubElement(decl[0], "{" + X + "}restriction", base="d:Text")
    elif mutation == "embedded_vendor":
        etree.SubElement(schema, "{" + X + "}schema", targetNamespace=DMS)
    elif mutation == "schema_default":
        schema[1].set("finalDefault", "#all")
    elif mutation == "instance_type":
        leaf.set("{" + values.XSI + "}type", "d:Text")
    elif mutation == "instance_attribute":
        leaf.set("secret", "Fictional secret")
    elif mutation == "structured":
        etree.SubElement(leaf, "{" + F + "}child").text = "Fictional secret"
    elif mutation == "PI":
        leaf.append(etree.ProcessingInstruction("fictional", "Fictional secret"))
    elif mutation == "duplicate_maximum":
        for _ in range(2):
            etree.SubElement(restriction, "{" + X + "}maxLength", value="10")
    elif mutation == "inconsistent_enumeration":
        etree.SubElement(restriction, "{" + X + "}enumeration", value=" ")
    elif mutation == "unknown_facet_attribute":
        etree.SubElement(restriction, "{" + X + "}enumeration", value="Fictional", unknown="secret")
    elif mutation == "many_facets":
        for _ in range(values.MAX_FACETS + 1):
            etree.SubElement(restriction, "{" + X + "}enumeration", value="Fictional")
    result = field(model(roots))["value_check"]
    assert result["status"] == "unsupported", result
    assert "Fictional" not in json.dumps(result)


@pytest.mark.parametrize("nil,nillable,text,status", [
    ("true", "true", None, "consistent_nil_shape"), (" 1 ", "1", None, "consistent_nil_shape"),
    ("true", "false", None, "invalid_value"), ("true", "true", " ", "invalid_value"),
    ("TRUE", "true", None, "invalid_value"), ("false", "true", "Fictional", "matches_reference_profile"),
])
def test_nil_is_separate_from_scalar_validity(nil, nillable, text, status):
    roots, _, _ = scalar("Text", text)
    declaration(roots).set("nillable", nillable)
    leaf = list(roots.values())[1].find("documentManagement/" + Q)
    leaf.set("{" + values.XSI + "}nil", nil)
    result = field(model(roots))["value_check"]
    assert result["status"] == status
    assert not result["full_type_validity_proven"]


def test_comments_keep_character_content_without_coercing_structured_values():
    roots, _, _ = scalar("Boolean", "tr")
    leaf = list(roots.values())[1].find("documentManagement/" + Q)
    comment = etree.Comment("Fictional private comment")
    comment.tail = "ue"
    leaf.append(comment)
    assert field(model(roots))["value_check"]["lexical_match"] is True


def test_long_values_are_unproved_without_large_number_conversion_or_disclosure():
    result = check("Lookup", "7" * (values.MAX_VALUE_CHARS + 1))
    assert result["status"] == "unsupported"
    assert result["reason_code"] == "value_length_bound"
    assert len(json.dumps(result)) < 600


def test_readonly_inventory_keeps_all_export_blockers_without_compilation(monkeypatch):
    for valid in [True, False]:
        roots, _, _ = scalar("Lookup", "7" if valid else "0")
        payload = package(extras=[(name, "application/xml", r) for name, r in roots.items()])
        source_hash = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(etree, "XMLSchema", lambda *a, **kw: pytest.fail("must not compile"))
        inv = revision_inventory.inventory_package_revisions(payload)
        diagnostic = inv["sharepoint_schema_model"]
        assert diagnostic["field_mapping_complete"]
        assert field(diagnostic)["value_check"]["lexical_match"] is valid
        assert not inv["clean_eligible"]
        assert not diagnostic["schema_validated"] and not diagnostic["clean_export_authorized"]
        with pytest.raises(clean_copy.ApplyError) as caught:
            clean_copy.prepare_clean_copy(payload, expected_source_sha256=source_hash)
        assert caught.value.code == "clean_copy_unsupported"
        assert hashlib.sha256(payload).hexdigest() == source_hash


@pytest.mark.parametrize("kind,profile", [("string", "string"), ("boolean", "boolean"),
    ("positiveInteger", "positive"), ("decimal", "decimal"), ("double", "double"), ("dateTime", "date")])
def test_checked_builtin_subset_agrees_with_independent_xsd_oracle(kind, profile):
    # Compile only this separately authored test schema, never document schemas.
    xsd = f'<x:schema xmlns:x="{X}"><x:element name="v" type="x:{kind}"/></x:schema>'
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    validator = etree.XMLSchema(etree.fromstring(xsd.encode(), parser))
    rng = random.Random(984521)
    cases = ["", "true", "TRUE", "1", "0", "-0", "+0007", "NaN", "-INF", "+INF",
             "1e2", "4.", ".5", "2026-10-25T00:00:00Z", "2100-02-29T00:00:00Z",
             "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"]
    cases += ["".join(rng.choices("0123456789+-eE. aNIFtrus\t\n\u00a0", k=rng.randrange(0, 30))) for _ in range(120)]
    for raw in cases:
        observed = values._lexical(raw, profile)
        if observed is None:
            continue
        if kind == "double" and raw.strip(values.XML_SPACE).endswith(("e", "E", "e+", "E+", "e-", "E-")):
            # libxml2 2.14.6 accepts a missing exponent. W3C 3.2.5.1 requires
            # an integer exponent; the literal controls above own this case.
            assert observed is False
            continue
        root = etree.Element("v")
        root.text = raw
        assert observed is validator.validate(root), (kind, repr(raw), observed)


def test_repeated_properties_reuse_profile_and_import_index(monkeypatch):
    roots, restriction, _ = scalar("Text", "Fictional")
    etree.SubElement(restriction, "{" + X + "}maxLength", value="10")
    manager = list(roots.values())[1][0]
    for _ in range(60):
        manager.append(copy.deepcopy(manager[0]))
    original = values._profile
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(values, "_profile", counted)
    result = model(roots)
    assert len(result["properties"][0]["fields"]) == 61
    assert len(calls) == 1
    assert not result["field_mapping_complete"]  # Duplicate properties are not valid mapping.
