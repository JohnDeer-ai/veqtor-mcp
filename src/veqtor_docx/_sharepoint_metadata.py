# SPDX-License-Identifier: Apache-2.0
"""Read-only SharePoint field mapping, deliberately separate from export proof.

This is not an XSD validator. In particular, a reserved DMS/InfoPath type is
never inferred to be a string, and a unique namespace match is not an OPC role
or consumer proof. Call only with the inventory's already bounded XML forest.
"""
from __future__ import annotations

import re

from lxml import etree

from ._ooxml import ResourceLimitError
from ._sharepoint_values import scalar_candidate_check

CONTENT_TYPE = "http://schemas.microsoft.com/office/2006/metadata/contentType"
PROPERTIES = "http://schemas.microsoft.com/office/2006/metadata/properties"
META = PROPERTIES + "/metaAttributes"
XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAX_MODEL_NODES = 20000
MAX_MODEL_FIELDS = 2048
MAX_MODEL_WORK = 500000
MAX_QNAME_CHARS = 2048
MAX_MODEL_OUTPUT_BYTES = 2 * 1024 * 1024
_XML_SPACE = " \t\r\n"


def _bound_output(value):
    """Bound JSON expansion before serialization, including repeated aliases.

    Reference arrays are shared internally but JSON repeats them for every row.
    Visit those occurrences lazily with a conservative ensure_ascii byte bound;
    do not materialize a large encoded report just to measure its size.
    """
    def entries(mapping):
        for key, child in mapping.items():
            yield key
            yield child

    frames, observed = [iter((value,))], 0
    while frames:
        try:
            item = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        if isinstance(item, dict):
            observed += 2 + 4 * len(item)
            frames.append(entries(item))
        elif isinstance(item, list):
            observed += 2 + 2 * len(item)
            frames.append(iter(item))
        elif isinstance(item, str):
            # An astral code point may become two escaped UTF-16 surrogates.
            observed += 2 + 12 * len(item)
        else:
            observed += 8  # Only bool/None leaves are produced by this model.
        _Model.limit("sharepoint_model_output", MAX_MODEL_OUTPUT_BYTES, observed)


def _q(namespace, local):
    return "{" + namespace + "}" + local


def _ncname(value):
    if not value or any(c in value for c in ":{}"):
        return False
    try:
        return etree.QName(value).localname == value
    except ValueError:
        return False


class _Model:
    def __init__(self):
        self.work = self.nodes = self.fields = 0
        self.issues = []
        self.references = {}
        self.schema_nodes = set()
        self.schema_namespaces = set()
        self.schema_imports = {}
        self.value_profiles = {}

    def charge(self, amount=1):
        self.work += amount
        self.limit("sharepoint_model_work", MAX_MODEL_WORK, self.work)

    @staticmethod
    def limit(name, allowed, observed):
        if observed > allowed:
            raise ResourceLimitError(name, "SharePoint diagnostic exceeds its bound",
                                     allowed_count=allowed, observed_count=observed)

    def issue(self, code, part, **detail):
        self.charge(1 + sum(len(str(v)) for v in detail.values()))
        self.issues.append({"code": code, "part_name": part, **detail})

    def qname(self, value, active, part):
        self.limit("sharepoint_qname_chars", MAX_QNAME_CHARS, len(value))
        self.charge(len(value))
        tokens = value.split(":")
        if len(tokens) == 1 and _ncname(tokens[0]):
            prefix, local = "", tokens[0]
        elif len(tokens) == 2 and all(_ncname(t) for t in tokens):
            prefix, local = tokens
            if prefix not in active:
                self.issue("unresolved_qname", part)
                return None
        else:
            self.issue("invalid_qname", part)
            return None
        namespace = active.get(prefix, "")
        expanded = _q(namespace, local) if namespace else local
        self.limit("sharepoint_qname_chars", MAX_QNAME_CHARS, len(expanded))
        self.charge(len(expanded))
        return expanded

    def index(self, root, part):
        # Namespace undo frames avoid copying the full inherited nsmap for every
        # field. Each declaration/attribute/reference is visited a bounded number
        # of times, including malicious wide or deeply rebound namespace sets.
        active, pending, frames = {"xml": "http://www.w3.org/XML/1998/namespace"}, [], []
        schema_mode = root.tag == _q(CONTENT_TYPE, "contentTypeSchema")
        annotation_depth = 0
        missing = object()
        for event, value in etree.iterwalk(root, events=("start-ns", "start", "end")):
            if event == "start-ns":
                prefix, uri = value
                prefix = prefix or ""
                self.charge(len(prefix) + len(uri) + 1)
                pending.append((prefix, active.get(prefix, missing)))
                active[prefix] = uri
            elif event == "end":
                if schema_mode and value.tag == _q(XSD, "annotation"):
                    annotation_depth -= 1
                for prefix, old in reversed(frames.pop()):
                    if old is missing:
                        active.pop(prefix, None)
                    else:
                        active[prefix] = old
            else:
                frames.append(pending)
                pending = []
                self.nodes += 1
                self.limit("sharepoint_model_nodes", MAX_MODEL_NODES, self.nodes)
                self.charge(1 + len(value.tag) + len(value.attrib))
                if schema_mode and value.tag == _q(XSD, "annotation"):
                    annotation_depth += 1
                schema_node = schema_mode and not annotation_depth and value.tag.startswith("{" + XSD + "}")
                if schema_node:
                    self.schema_nodes.add(value)
                refs = []
                if schema_node and value.tag in {_q(XSD, k) for k in ("include", "redefine", "override")}:
                    self.issue("unresolved_schema_dependency", part)
                for attr, raw in value.attrib.items():
                    self.charge(len(attr) + len(raw))
                    if ((schema_node and attr in {"ref", "type", "base", "itemType", "memberTypes"})
                            or (not schema_mode and attr == _q(XSI, "type"))):
                        values = re.split(r"[ \t\r\n]+", raw.strip(_XML_SPACE)) if attr == "memberTypes" else [raw]
                        if not values:
                            self.issue("invalid_qname", part)
                        for token in values:
                            refs.append({"attribute": attr, "qname": self.qname(token, active, part)})
                    if (not annotation_depth and
                            ((schema_node and attr == "schemaLocation") or
                             attr in {_q(XSI, "schemaLocation"), _q(XSI, "noNamespaceSchemaLocation")})):
                        self.issue("unresolved_schema_dependency", part)
                self.references[value] = refs

    def child(self, node, tag, part):
        children = [n for n in node if n.tag != _q(XSD, "annotation")]
        self.charge(len(node) + 1)
        if len(children) == 1 and children[0].tag == _q(XSD, tag):
            return children[0]
        self.issue("unsupported_root_declaration_shape", part)
        return None

    def schema(self, root, part):
        self.index(root, part)
        declarations, schemas, root_schemas = {}, {}, []
        imports, defaults = [], []
        for schema in root:
            self.charge()
            if schema.tag != _q(XSD, "schema"):
                self.issue("unsupported_schema_set_child", part)
                continue
            namespace = schema.get("targetNamespace")
            self.schema_namespaces.add(namespace)
            if not namespace or namespace in schemas:
                self.issue("missing_or_duplicate_schema_namespace", part)
            schemas[namespace] = schema
            self.schema_imports[schema] = {}
            root_flag = schema.get(_q(META, "root"))
            if root_flag in {"true", "1"}:
                root_schemas.append(schema)
            elif root_flag is not None and root_flag not in {"false", "0"}:
                self.issue("invalid_root_flag", part)
            for node in schema:
                self.charge()
                if node.tag == _q(XSD, "import"):
                    self.schema_imports[schema].setdefault(node.get("namespace"), []).append(node)
                    imports.append({"namespace": node.get("namespace"),
                                    "location_present": "schemaLocation" in node.attrib})
                elif node.tag == _q(XSD, "element"):
                    self.fields += 1
                    self.limit("sharepoint_model_fields", MAX_MODEL_FIELDS, self.fields)
                    local = node.get("name")
                    if not namespace or not _ncname(local):
                        self.issue("invalid_global_declaration", part)
                        continue
                    name = _q(namespace, local)
                    self.charge(len(name))
                    if name in declarations:
                        self.issue("duplicate_field_declaration", part, qname=name)
                    declarations.setdefault(name, []).append(node)
            for node in schema.iter():
                self.charge()
                if node in self.schema_nodes and any(a in node.attrib for a in ("default", "fixed", _q(META, "default"))):
                    defaults.append({"schema_namespace": namespace,
                                     "declaration_name": node.get("name"),
                                     "default_present": "default" in node.attrib,
                                     "metadata_default_present": _q(META, "default") in node.attrib,
                                     "fixed_present": "fixed" in node.attrib})
        manager, refs = None, {}
        if len(root_schemas) != 1 or root_schemas[0].get("targetNamespace") != PROPERTIES:
            self.issue("missing_or_ambiguous_root_schema", part)
        else:
            schema = root_schemas[0]
            if not schema.get(_q(META, "fieldsID")):
                self.issue("missing_fields_identity", part)
            options = declarations.get(_q(PROPERTIES, "properties"), [])
            node = options[0] if len(options) == 1 else None
            if node is None:
                self.issue("missing_or_ambiguous_properties_declaration", part)
            for tag in ("complexType", "sequence", "element"):
                node = self.child(node, tag, part) if node is not None else None
            if node is not None:
                form = node.get("form", schema.get("elementFormDefault", "unqualified"))
                if node.get("name") != "documentManagement" or "ref" in node.attrib or form not in {"qualified", "unqualified"}:
                    self.issue("unsupported_root_container", part)
                else:
                    manager = _q(PROPERTIES, "documentManagement") if form == "qualified" else "documentManagement"
                for tag in ("complexType", "all"):
                    node = self.child(node, tag, part) if node is not None else None
                if node is not None:
                    for field in node:
                        self.charge()
                        if field.tag == _q(XSD, "annotation"):
                            continue
                        resolved = [r["qname"] for r in self.references.get(field, []) if r["attribute"] == "ref"]
                        if field.tag != _q(XSD, "element") or len(resolved) != 1 or resolved[0] is None:
                            self.issue("unresolved_root_field_reference", part)
                            continue
                        name = resolved[0]
                        minimum = field.get("minOccurs", "1")
                        if name in refs or minimum not in {"0", "1"} or field.get("maxOccurs", "1") != "1":
                            self.issue("duplicate_or_unsupported_root_field_reference", part, qname=name)
                        refs[name] = minimum
                        if len(declarations.get(name, [])) != 1:
                            self.issue("missing_or_ambiguous_field_declaration", part, qname=name)
        if "http://schemas.openxmlformats.org/package/2006/metadata/core-properties" not in schemas:
            self.issue("missing_core_properties_schema", part)
        # Store only shape and QName facts, never field/default values or labels.
        fields = {}
        for name, options in declarations.items():
            if len(options) != 1:
                continue
            declaration = options[0]
            type_refs = []
            element_refs = []
            for node in declaration.iter():
                self.charge()
                type_refs.extend(r for r in self.references.get(node, []) if r["attribute"] in {"type", "base", "itemType", "memberTypes"})
                element_refs.extend(r["qname"] for r in self.references.get(node, []) if r["attribute"] == "ref")
            fields[name] = {"nillable": declaration.get("nillable") in {"true", "1"},
                            "declaration": declaration,
                            "type_references": type_refs, "element_references": element_refs}
        return {"part_name": part, "document_management_qname": manager,
                "root_field_references": sorted(refs), "imports": imports,
                "defaults_or_fixed": defaults}, fields, refs

    def properties(self, root, part, schema_row, fields, refs):
        self.index(root, part)
        manager_name = schema_row["document_management_qname"]
        managers = [n for n in root if n.tag == manager_name]
        if (len(managers) != 1 or len(root) != 1 or root.attrib
                or (root.text or "").strip()):
            self.issue("unproved_properties_container", part)
        rows, seen = [], set()
        for manager in managers:
            if manager.attrib or (manager.text or "").strip() or (manager.tail or "").strip():
                self.issue("unproved_properties_container", part)
            for node in manager:
                self.charge()
                if not isinstance(node.tag, str):
                    self.issue("unsupported_property_node", part)
                    continue
                name = node.tag
                row = {"qname": name, "status": "missing_or_ambiguous_declaration",
                       "type_semantics_proven": False}
                declaration = fields.get(name)
                instance_types = [r["qname"] for r in self.references.get(node, [])
                                  if r["attribute"] == _q(XSI, "type")]
                if instance_types:
                    row["instance_type_qname"] = instance_types[0]
                if name in seen:
                    self.issue("duplicate_property", part, qname=name)
                seen.add(name)
                if declaration is None or name not in refs:
                    self.issue("missing_or_ambiguous_property_declaration", part, qname=name)
                else:
                    row["value_check"] = scalar_candidate_check(
                        node, declaration["declaration"], self.references,
                        self.schema_namespaces, self.schema_imports, self.value_profiles, self.charge,
                    )
                    row["type_references"] = declaration["type_references"]
                    row["element_references"] = declaration["element_references"]
                    nil = node.get(_q(XSI, "nil"))
                    if nil is not None:
                        nil = nil.strip(_XML_SPACE)
                    if nil in {"true", "1"}:
                        content = node.text or any(isinstance(child.tag, str) or child.tail for child in node)
                        if not declaration["nillable"] or content:
                            row["status"] = "inconsistent_explicit_nil"
                            self.issue("inconsistent_explicit_nil", part, qname=name)
                        elif set(node.attrib) - {_q(XSI, "nil"), _q(XSI, "type")}:
                            row["status"] = "explicit_nil_shape_requires_attribute_semantics"
                        else:
                            row["status"] = "explicit_nil_shape"
                    elif nil is not None and nil not in {"false", "0"}:
                        row["status"] = "invalid_nil_flag"
                        self.issue("invalid_nil_flag", part, qname=name)
                    else:
                        row["status"] = "structured_value_requires_type_semantics" if len(node) else "scalar_value_requires_type_semantics"
                    if (node.tail or "").strip():
                        self.issue("mixed_property_content", part, qname=name)
                rows.append(row)
        for name, minimum in refs.items():
            self.charge()
            if minimum == "1" and name not in seen:
                self.issue("missing_required_property", part, qname=name)
        return {"part_name": part, "fields": rows}


def sharepoint_schema_model(parsed_roots):
    """Return an optional advisory model; existing export blockers stay intact.

    Association requires a single schema set and a single properties root. This
    is explicitly only a namespace candidate, never a package-binding proof.
    Missing or multiple stores produce diagnostics rather than guessed pairings.
    """
    schemas = [(n, r) for n, r in sorted(parsed_roots.items()) if r.tag == _q(CONTENT_TYPE, "contentTypeSchema")]
    properties = [(n, r) for n, r in sorted(parsed_roots.items()) if r.tag == _q(PROPERTIES, "properties")]
    if not schemas and not properties:
        return None
    model = _Model()
    schema_rows, indexed = [], []
    for part, root in schemas:
        row, fields, refs = model.schema(root, part)
        schema_rows.append(row)
        indexed.append((row, fields, refs))
    property_rows = []
    if len(schemas) != 1 or len(properties) != 1:
        model.issue("missing_or_ambiguous_schema_property_pair", "[package]")
        for part, root in properties:
            model.index(root, part)
            property_rows.append({"part_name": part, "fields": [], "association": "unresolved"})
    else:
        part, root = properties[0]
        property_rows.append(model.properties(root, part, *indexed[0]))
    result = {"schema_version": "sharepoint_schema_model.v1", "diagnostic_only": True,
            "association": "unique_namespace_candidate" if len(schemas) == len(properties) == 1 else "unresolved",
            "schema_validated": False, "schema_compiled": False,
            "external_schema_fetched": False, "consumer_effect_proven": False,
            "package_binding_proven": False, "clean_export_authorized": False,
            "field_mapping_complete": not model.issues,
            "schemas": schema_rows, "properties": property_rows, "issues": model.issues}
    _bound_output(result)
    return result
