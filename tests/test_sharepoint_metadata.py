# SPDX-License-Identifier: Apache-2.0
"""Literal, fictional SharePoint maps; a map never authorizes clean export."""
import copy
import hashlib

import pytest
from lxml import etree

from veqtor_docx import _sharepoint_metadata as metadata
from veqtor_docx import clean_copy, revision_inventory
from veqtor_docx._ooxml import ResourceLimitError
from test_revision_inventory import package, xml

X, P, C, MA = metadata.XSD, metadata.PROPERTIES, metadata.CONTENT_TYPE, metadata.META
F = "urn:veqtor:fictional:sharepoint-fields"
DMS = "http://schemas.microsoft.com/office/2006/documentManagement/types"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
Q = "{" + F + "}FictionalLabel"


def fixture(*, form=None, local_form=None, nil=None, value="Fictional private value"):
    schema = xml(f'''<c:contentTypeSchema xmlns:c="{C}" xmlns:x="{X}" xmlns:ma="{MA}">
      <x:schema targetNamespace="{P}" xmlns:f="{F}" ma:root="true" ma:fieldsID="fictional-fields">
        <x:import namespace="{F}"/>
        <x:element name="properties"><x:complexType><x:sequence>
          <x:element name="documentManagement"><x:complexType><x:all>
            <x:element ref="f:FictionalLabel" minOccurs="0"/>
          </x:all></x:complexType></x:element>
        </x:sequence></x:complexType></x:element>
      </x:schema>
      <x:schema targetNamespace="{F}" xmlns:d="{DMS}">
        <x:import namespace="{DMS}"/>
        <x:element name="FictionalLabel" nillable="true" ma:index="0">
          <x:simpleType><x:restriction base="d:Text"/></x:simpleType>
        </x:element>
      </x:schema>
      <x:schema targetNamespace="{CP}"/>
    </c:contentTypeSchema>''')
    local = next(n for n in schema[0].iter("{" + X + "}element") if n.get("name") == "documentManagement")
    if form is not None:
        schema[0].set("elementFormDefault", form)
    if local_form is not None:
        local.set("form", local_form)
    props = etree.Element("{" + P + "}properties", nsmap={"p": P, "f": F, "i": metadata.XSI})
    effective_form = local_form or form or "unqualified"
    manager = etree.SubElement(props, "{" + P + "}documentManagement" if effective_form == "qualified" else "documentManagement")
    leaf = etree.SubElement(manager, Q)
    if nil is not None:
        leaf.set("{" + metadata.XSI + "}nil", nil)
    leaf.text = value
    return {"records/fictional-schema.xml": schema, "records/fictional-properties.xml": props}


def model(roots):
    snapshots = {n: etree.tostring(r) for n, r in roots.items()}
    result = metadata.sharepoint_schema_model(roots)
    assert snapshots == {n: etree.tostring(r) for n, r in roots.items()}
    return result


def issue_codes(result):
    return {r["code"] for r in result["issues"]}


def field(result):
    return result["properties"][0]["fields"][0]


def declaration(roots):
    return roots["records/fictional-schema.xml"][1][1]


@pytest.mark.parametrize("form,local_form,expected", [(None, None, "documentManagement"),
    ("qualified", None, "{" + P + "}documentManagement"),
    ("unqualified", "qualified", "{" + P + "}documentManagement"),
    ("qualified", "unqualified", "documentManagement")])
def test_literal_mapping_derives_local_qualification(form, local_form, expected):
    roots = fixture(form=form, local_form=local_form)
    result = model(roots)
    assert result["field_mapping_complete"], result
    assert result["schemas"][0]["document_management_qname"] == expected
    assert result["schemas"][0]["root_field_references"] == [Q]
    assert field(result) == {"qname": Q, "status": "scalar_value_requires_type_semantics",
                             "type_semantics_proven": False,
                             "element_references": [],
                             "type_references": [{"attribute": "base", "qname": "{" + DMS + "}Text"}]}
    for flag in ("schema_validated", "schema_compiled", "external_schema_fetched", "consumer_effect_proven",
                 "package_binding_proven", "clean_export_authorized"):
        assert result[flag] is False
    assert "Fictional private value" not in str(result)


@pytest.mark.parametrize("nil,value,status", [("true", None, "explicit_nil_shape"),
    ("1", None, "explicit_nil_shape"), (None, None, "scalar_value_requires_type_semantics"),
    ("false", None, "scalar_value_requires_type_semantics"), ("0", "", "scalar_value_requires_type_semantics"),
    ("true", " ", "inconsistent_explicit_nil"), ("true", "Fictional", "inconsistent_explicit_nil"),
    ("future", None, "invalid_nil_flag")])
def test_nil_is_not_inferred_from_empty_text(nil, value, status):
    result = model(fixture(nil=nil, value=value))
    assert field(result)["status"] == status


@pytest.mark.parametrize("mutation", ["not_nillable", "child", "extra_attribute"])
def test_explicit_nil_requires_consistent_declaration_and_empty_shape(mutation):
    roots = fixture(nil="true", value=None)
    leaf = roots["records/fictional-properties.xml"][0][0]
    if mutation == "not_nillable":
        declaration(roots).set("nillable", "false")
    elif mutation == "child":
        etree.SubElement(leaf, "fictional")
    else:
        leaf.set("future", "true")
    result = model(roots)
    assert not result["field_mapping_complete"]
    assert field(result)["status"] == "inconsistent_explicit_nil"


def test_structured_reserved_value_is_preserved_as_unproved():
    roots = fixture(value=None)
    etree.SubElement(roots["records/fictional-properties.xml"][0][0], "{" + F + "}Term").text = "Fictional term"
    assert field(model(roots))["status"] == "structured_value_requires_type_semantics"


def test_nested_element_refs_are_retained_without_guessing_type_semantics():
    roots = fixture(value=None)
    decl = declaration(roots)
    decl.remove(decl[0])
    complex_type = etree.SubElement(decl, "{" + X + "}complexType", nsmap={"pc": "urn:fictional:partner-controls"})
    sequence = etree.SubElement(complex_type, "{" + X + "}sequence")
    etree.SubElement(sequence, "{" + X + "}element", ref="pc:Terms", minOccurs="0")
    result = model(roots)
    assert field(result)["element_references"] == ["{urn:fictional:partner-controls}Terms"]
    assert field(result)["type_references"] == []
    assert not field(result)["type_semantics_proven"]


@pytest.mark.parametrize("attribute", ["default", "fixed", "{" + MA + "}default"])
def test_schema_and_metadata_defaults_report_presence_without_values(attribute):
    roots = fixture()
    declaration(roots).set(attribute, "Private fictional default")
    result = model(roots)
    assert result["schemas"][0]["defaults_or_fixed"] == [{
        "schema_namespace": F, "declaration_name": "FictionalLabel",
        "default_present": attribute == "default", "fixed_present": attribute == "fixed",
        "metadata_default_present": attribute == "{" + MA + "}default"}]
    assert "Private fictional default" not in str(result)


@pytest.mark.parametrize("lexical,namespace,expected", [
    ("f:FictionalAlternative", {"f": F}, "{" + F + "}FictionalAlternative"),
    ("missing:Unknown", {}, None)])
def test_instance_type_override_is_not_hidden_by_declared_type(lexical, namespace, expected):
    roots = fixture()
    manager = roots["records/fictional-properties.xml"][0]
    manager.remove(manager[0])
    leaf = etree.SubElement(manager, Q, nsmap=namespace)
    leaf.set("{" + metadata.XSI + "}type", lexical)
    leaf.text = "Fictional value"
    result = model(roots)
    assert field(result)["instance_type_qname"] == expected
    assert not field(result)["type_semantics_proven"]
    if expected is None:
        assert not result["field_mapping_complete"]


@pytest.mark.parametrize("lexical,namespace,expected", [
    ("renamed:Text", {"renamed": DMS}, "{" + DMS + "}Text"),
    ("d:Text", {"d": "urn:fictional:rebound"}, "{urn:fictional:rebound}Text"),
    ("Text", {None: DMS}, "{" + DMS + "}Text"), ("Text", {}, "Text")])
def test_qname_uses_exact_local_namespace_scope(lexical, namespace, expected):
    roots = fixture()
    parent = declaration(roots)[0]
    parent.remove(parent[0])
    etree.SubElement(parent, "{" + X + "}restriction", nsmap=namespace, base=lexical)
    assert field(model(roots))["type_references"] == [{"attribute": "base", "qname": expected}]


@pytest.mark.parametrize("value", ["missing:Text", "d:Text:Other", "{urn:fake}Text", "", "has space"])
def test_malformed_and_unbound_type_qnames_are_explicit(value):
    roots = fixture()
    declaration(roots)[0][0].set("base", value)
    result = model(roots)
    assert not result["field_mapping_complete"]
    assert issue_codes(result) & {"invalid_qname", "unresolved_qname"}
    assert field(result)["type_references"] == [{"attribute": "base", "qname": None}]


@pytest.mark.parametrize("mutation,code", [
    ("root_flag", "missing_or_ambiguous_root_schema"),
    ("duplicate_root", "missing_or_ambiguous_root_schema"),
    ("duplicate_schema", "missing_or_duplicate_schema_namespace"),
    ("duplicate_field", "duplicate_field_declaration"),
    ("missing_field", "missing_or_ambiguous_field_declaration"),
    ("duplicate_ref", "duplicate_or_unsupported_root_field_reference"),
    ("missing_core", "missing_core_properties_schema"),
    ("missing_identity", "missing_fields_identity"),
    ("wrong_manager", "unproved_properties_container"),
    ("wrong_field_namespace", "missing_or_ambiguous_property_declaration"),
    ("extra_manager", "unproved_properties_container"),
    ("duplicate_property", "duplicate_property"),
    ("mixed_content", "mixed_property_content"),
    ("missing_required", "missing_required_property"),
    ("unknown_schema_child", "unsupported_schema_set_child"),
    ("include", "unresolved_schema_dependency"),
    ("location", "unresolved_schema_dependency"),
    ("root_shape", "unsupported_root_declaration_shape")])
def test_ambiguous_missing_and_unsupported_mapping_is_not_complete(mutation, code):
    roots = fixture()
    schema, props = roots.values()
    all_fields = next(schema.iter("{" + X + "}all"))
    if mutation == "root_flag":
        schema[0].attrib.pop("{" + MA + "}root")
    elif mutation == "duplicate_root":
        schema[1].set("{" + MA + "}root", "true")
    elif mutation == "duplicate_schema":
        schema.append(copy.deepcopy(schema[1]))
    elif mutation == "duplicate_field":
        schema[1].append(copy.deepcopy(declaration(roots)))
    elif mutation == "missing_field":
        schema[1].remove(declaration(roots))
    elif mutation == "duplicate_ref":
        all_fields.append(copy.deepcopy(all_fields[0]))
    elif mutation == "missing_core":
        schema.remove(schema[-1])
    elif mutation == "missing_identity":
        schema[0].attrib.pop("{" + MA + "}fieldsID")
    elif mutation == "wrong_manager":
        props[0].tag = "{" + P + "}documentManagement"
    elif mutation == "wrong_field_namespace":
        props[0][0].tag = "{urn:fictional:other}FictionalLabel"
    elif mutation == "extra_manager":
        etree.SubElement(props, "future")
    elif mutation == "duplicate_property":
        props[0].append(copy.deepcopy(props[0][0]))
    elif mutation == "mixed_content":
        props[0][0].tail = "Unexpected mixed content"
    elif mutation == "missing_required":
        all_fields[0].attrib.pop("minOccurs")
        props[0].remove(props[0][0])
    elif mutation == "unknown_schema_child":
        etree.SubElement(schema, "{urn:fictional:future}schema")
    elif mutation == "include":
        etree.SubElement(schema[1], "{" + X + "}include")
    elif mutation == "location":
        schema[0][0].set("schemaLocation", "https://invalid.example/never-fetch.xsd")
    else:
        etree.SubElement(schema[0][1][0], "{" + X + "}attribute", name="unexpected")
    result = model(roots)
    assert not result["field_mapping_complete"]
    assert code in issue_codes(result)


@pytest.mark.parametrize("kind", ["missing_schema", "missing_properties", "two_schemas", "two_properties"])
def test_pairing_never_chooses_by_filename_or_first_match(kind):
    roots = fixture()
    if kind == "missing_schema":
        roots.pop("records/fictional-schema.xml")
    elif kind == "missing_properties":
        roots.pop("records/fictional-properties.xml")
    else:
        name = "records/fictional-schema.xml" if kind == "two_schemas" else "records/fictional-properties.xml"
        roots["records/ambiguous.xml"] = copy.deepcopy(roots[name])
    result = model(roots)
    assert not result["field_mapping_complete"]
    assert result["association"] == "unresolved"
    assert "missing_or_ambiguous_schema_property_pair" in issue_codes(result)
    assert all(r["fields"] == [] for r in result["properties"])


def test_inventory_integration_preserves_refusal_and_bytes_without_schema_execution(monkeypatch):
    roots = fixture()
    source = package(extras=[(name, "application/xml", r) for name, r in roots.items()])
    monkeypatch.setattr(etree, "XMLSchema", lambda *a, **k: pytest.fail("must not compile"))
    before = bytes(source)
    result = revision_inventory.inventory_package_revisions(source)
    assert result["sharepoint_schema_model"]["field_mapping_complete"]
    assert result["sharepoint_schema_model"]["association"] == "unique_namespace_candidate"
    assert not result["clean_eligible"]
    assert not result["complete"]
    with pytest.raises(clean_copy.ApplyError) as caught:
        clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    assert caught.value.code == "clean_copy_unsupported"
    assert source == before


def test_ordinary_inventory_is_unchanged_and_no_part_name_inference():
    assert metadata.sharepoint_schema_model({"customXml/schema.xml": xml('<fictional/>')}) is None
    assert "sharepoint_schema_model" not in revision_inventory.inventory_package_revisions(package())


@pytest.mark.parametrize("bound,value,limit", [("MAX_MODEL_NODES", 10, "sharepoint_model_nodes"),
    ("MAX_MODEL_FIELDS", 1, "sharepoint_model_fields"), ("MAX_MODEL_WORK", 100, "sharepoint_model_work"),
    ("MAX_QNAME_CHARS", 2, "sharepoint_qname_chars")])
def test_diagnostic_limits_are_controlled_and_read_only(monkeypatch, bound, value, limit):
    roots = fixture()
    snapshots = {n: etree.tostring(r) for n, r in roots.items()}
    monkeypatch.setattr(metadata, bound, value)
    with pytest.raises(ResourceLimitError) as caught:
        metadata.sharepoint_schema_model(roots)
    assert caught.value.limit == limit
    assert snapshots == {n: etree.tostring(r) for n, r in roots.items()}


@pytest.mark.parametrize("count", [64, 128, 256])
def test_wide_maps_use_bounded_linear_work_without_value_disclosure(count, monkeypatch):
    instances = []
    original = metadata._Model.__init__
    def observe(self):
        original(self)
        instances.append(self)
    monkeypatch.setattr(metadata._Model, "__init__", observe)
    roots = fixture(value=None)
    schema, props = roots.values()
    all_fields = next(schema.iter("{" + X + "}all"))
    refs_template, field_template = copy.deepcopy(all_fields[0]), copy.deepcopy(declaration(roots))
    all_fields.remove(all_fields[0])
    schema[1].remove(declaration(roots))
    props[0].remove(props[0][0])
    for i in range(count):
        name = f"FictionalField{i:04d}"
        ref, decl = copy.deepcopy(refs_template), copy.deepcopy(field_template)
        ref.set("ref", "f:" + name)
        decl.set("name", name)
        all_fields.append(ref)
        schema[1].append(decl)
        etree.SubElement(props[0], "{" + F + "}" + name).text = "Private fictional value"
    result = model(roots)
    assert result["field_mapping_complete"]
    assert len(result["properties"][0]["fields"]) == count
    # A work-unit oracle, independent of machine speed. The constant includes
    # namespace/attribute/QName characters, not only element visits.
    assert instances[0].work < 1800 * count + 10000
    assert "Private fictional value" not in str(result)


def test_many_inherited_namespaces_are_not_copied_per_field(monkeypatch):
    roots = fixture()
    schema = roots["records/fictional-schema.xml"]
    extra_namespaces = {f"fictional{i}": f"urn:fictional:namespace:{i}" for i in range(512)}
    outer = etree.Element(schema.tag, nsmap=extra_namespaces)
    outer.extend(copy.deepcopy(list(schema)))
    roots["records/fictional-schema.xml"] = outer
    result = model(roots)
    assert result["field_mapping_complete"]
    monkeypatch.setattr(metadata, "MAX_MODEL_WORK", 10000)
    with pytest.raises(ResourceLimitError) as caught:
        model(roots)
    assert caught.value.limit == "sharepoint_model_work"


@pytest.mark.parametrize("mutation", [None, "alias", "wrong_mime", "orphan"])
def test_even_attached_schema_pair_does_not_claim_binding_or_consumers(mutation):
    from test_user_xml_storage import fixture as storage_fixture, set_type, set_xml
    from test_revision_inventory import ODREL, REL, archive
    parts, data, _ = storage_fixture(count=2)
    schema, props = fixture().values()
    parts[data[0]], parts[data[1]] = etree.tostring(schema), etree.tostring(props)
    if mutation == "wrong_mime":
        set_type(parts, data[0], "text/xml")
    elif mutation == "alias":
        def add_alias(root):
            etree.SubElement(root, "{" + REL + "}Relationship", Id="rIdAlias", Type=ODREL + "customXml", Target="../" + data[0])
        set_xml(parts, "word/_rels/document.xml.rels", add_alias)
    elif mutation == "orphan":
        def remove_link(root):
            node = next(n for n in root if n.get("Target") == "../" + data[0])
            root.remove(node)
        set_xml(parts, "word/_rels/document.xml.rels", remove_link)
    payload = archive(parts)
    result = revision_inventory.inventory_package_revisions(payload)
    assert not result["clean_eligible"]
    model_result = result["sharepoint_schema_model"]
    assert model_result["field_mapping_complete"]
    assert not model_result["package_binding_proven"]
    assert not model_result["consumer_effect_proven"]
    assert all(r["inspection"] != "opaque_user_storage" for r in result["parts"])
