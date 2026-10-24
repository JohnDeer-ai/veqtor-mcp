<!-- SPDX-License-Identifier: Apache-2.0 -->
# Internal clean-copy engine (PR 8)

This stage implements a byte-in/byte-out candidate engine and a package-wide
revision inventory. It does not add an MCP tool, publish a file, replace a working
document, prepare an outgoing bundle or declare a document ready to send.
The connected private build is a separate immutable installation.

```python
import hashlib
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx.revision_inventory import inventory_package_revisions

inventory = inventory_package_revisions(source_bytes)
candidate = prepare_clean_copy(
    source_bytes,
    expected_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
)
# candidate.payload is a separate DOCX; a later publication layer owns its save.
```

`package_revisions.v1` inventories every ZIP member, with the declared content
type, inspection status, revisions and blockers. XML is identified by content
type as well as filename, including orphan XML parts. Each revision has a part
name, zero-based child-index path, kind and `supported_location`. This last flag
means only a known main-body type/location, not a validated operation. `complete`
describes inspection coverage; `clean_eligible` additionally requires no known
unsupported revisions. A complete inventory can therefore still block export.

The engine checks the entire original package before accepting anything. It
cannot hide unknown history by deleting its containing text or old property
set. It accepts supported canonical main-body text revisions, the seven existing
property domains (including row-level table-property exceptions), and the
existing complete paragraph/row structural units. Structural stages refresh
their references after each patch. Multiple disjoint structural units are
validated in a detached context with the other explicitly selected units accepted;
only the selected exact patch is applied to the candidate. Adjacent overlapping
paragraph units remain unsupported. Public selective-decision guards are unchanged.
Related table properties use the complete
selected group and final geometry checks. Existing style, numbering, field,
protection and shape refusals remain in force. Acceptance never reassigns the
original author's name or creates another tracked revision.

Every mutation must equal its planned local patch. Small and structural selections
use the selective path: reversing each patch in an independent document copy must
restore the exact previous XML. Larger nonstructural selections use a bounded
batch path. It checks the full source boundary grammar, proves each original
owner and formatting dependency, and runs the existing local text/property guards
in a context that retains its paragraph and ancestor property sets. Contexts with
unproved cross-paragraph field/range or nested-table dependencies fall back to the
full-context selective path. One whole-ledger reversal must restore the exact
source, and an independently replayed candidate must equal the planned outcome.
Complete selected table groups and their final geometry are still checked.
Style dependencies follow the selected cell, row or table owners, matching the
selective path. An untouched sibling cell's style is not a new dependency of a
cell-only change; a selected whole-table property still checks the whole table.
The final package
is inventoried again and must contain zero revision markers. Its member set and
every part other than `word/document.xml` remain byte-identical. An already clean
supported package is returned byte-for-byte, including its original ZIP bytes.
Comments, properties and tracking settings are retained; metadata cleaning and
explicit comment policies belong to PR 9. A touched bookmark/comment range
currently refuses rather than guessing how to preserve its binding. Unaffected
ranges remain in place.

The inventory deliberately has a bounded vocabulary. Revisions in headers,
footers, notes, comments, styles or any other external story block this version.
Unknown revision forms, unknown WordprocessingML/extension markup, alternate
representations, embedded Office/opaque parts and unresolved package declarations
also block. Known Word comment metadata and narrowly validated empty bibliography
metadata are recognized without deletion; unproved custom XML remains blocked.
Additional closed inspection profiles recognize the style `locked` on/off leaf,
web-division formatting, a bound `people` part, canonical modern comment-identity
content types and a bound SharePoint `FormTemplates` tree with the three documented
text leaves. These profiles check the complete supported subtree and package
binding; recognizing a namespace or `mc:Ignorable` token grants no permission.
Unknown children, attributes, values, bindings and dynamic custom schemas still
block export. Metadata recognition does not authorize comment mutation or deletion.
Processing instructions anywhere in an XML part and document nodes before or
after its root also block: root-only mutation proofs cannot establish their
semantics or preservation. XML declarations and comments inside the root remain
subject to their existing context checks; the shared document reader is unchanged.
The documented `w14:ligatures` leaf is recognized in its exact run-property
context. Selective text and run-property acceptance/rejection preserve its known
value or restore the recorded value; other OpenType extensions remain unsupported.
Only explicitly recognized raster-image content types and signatures are treated
as opaque revision-free media. This is an export gate, not a promise
that every legal DOCX or all hidden data is supported.

A separate first profile recognizes standard unconsumed user XML storage by
its complete OPC role, not by filenames or XML prefixes. It requires one implicit
main-document `customXml` edge, one exact `customXmlProps` edge, the fixed
properties MIME/root, globally unique braced GUIDs and exactly one explicit
`schemaRefs` element. An omitted element does not disable schema selection:
applications may then choose schemas by other means
([Microsoft reference](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.customxmldataproperties.schemareferences?view=openxml-3.0.1)).
All incoming aliases, unexpected outgoing edges, external/missing sources or
targets and orphan properties refuse. ASCII-case-equivalent package member or
override names also refuse, following OPC part-URI equivalence
([Microsoft reference](https://learn.microsoft.com/en-us/dotnet/core/compatibility/core-libraries/8.0/system-io-packaging-case-insensitive-uri));
no identity normalization or repair is performed. This identity-ambiguity gate
also applies to clean export without user storage, including zero-revision
packages; the shared reader and selective editing remain unchanged. The entire remaining package must have
recognized coverage before an item receives `inspection: opaque_user_storage`,
`preservation: exact_bytes_only` and its `properties_part`; unknown auxiliary or
story context cannot receive a complete-consumer-graph claim.

The consumer proof excludes fields (including split/deleted instructions),
controls/bindings, custom XML document markup, content-part/drawing alternatives,
MC markup and all stored `r:*` attributes anywhere in the package. The only MC
attribute exception is root-level `Ignorable` on a content-type-matched known Word
part. Prefixes must be nonempty, unique and in scope; core Word, XML, relationship,
content-type and MC namespaces cannot be declared ignorable in this profile.
Every actual element and attribute still needs existing inventory coverage.
The engine does not skip unfamiliar content, preprocess MC, select alternatives
or remove the annotation. Nested directives and all other MC constructs refuse.
This is a bounded configuration for the root annotation under ECMA-376 Part 3
sections 7.2 and 9.2, not general MC conformance
([standard](https://ecma-international.org/publications-and-standards/standards/ecma-376/)).
Plain literal words such as `DOCPROPERTY` are not fields. Office vocabulary in a
payload or its namespace bindings/attributes, reserved Office roots, unmatched or
multiple schema references, XSLT and schema imports/includes/locations remain
unsupported.
Other existing closed metadata profiles and documents without new user stores
retain their previous rules. This does not open dynamic Office, SharePoint,
InfoPath or Office Intelligence metadata.

An empty schema-reference list explicitly declares no associated schemas. The
Word-resaved form may instead retain one closed `schemaRef` whose namespace URI
exactly equals the payload root namespace. This narrow identifier syntax supports
simple URNs and HTTP(S) namespace identifiers; query/fragment/encoded and other
URI forms remain unsupported. A named reference is a validation association,
not an empty declaration or an assertion of schema validity
([Microsoft reference](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.customxmldataproperties.schemareference?view=openxml-3.0.1)).
The storage, reference and their package bindings stay byte-identical while body
decisions are applied. The already-excluded document consumers, unknown context
and external relationship rules remain necessary. The engine does not locate,
download, compile or validate against an associated schema and makes no claim
about external application validation, validity of the stored data or future Word
normalization. Association with a separate stored XSD is not inferred from its
filename or `targetNamespace`.

The data may include fresh generic XML or a separate standalone XSD stored only
as arbitrary data. The engine neither compiles nor fetches schemas, validates the
user payload's business meaning, normalizes it nor applies its values to text.
All payload/properties bytes, relationship and content-type parts remain exact
under the same full-package before/after proof. This is preserved opaque user
storage within the supported offline Word model, not a warranty of arbitrary
external application semantics or previously valid data. Global revision,
XML-envelope/DTD, depth and aggregate work limits still inspect every part.
The role is based on ECMA-376 Part 1 sections 15.2.5-15.2.6 and the pinned SDK
[CustomXmlPart](https://github.com/dotnet/Open-XML-SDK/blob/431ab05cf160248cc3885a4a766026d4f8243792/data/parts/CustomXmlPart.json)
and [CustomXmlPropertiesPart](https://github.com/dotnet/Open-XML-SDK/blob/431ab05cf160248cc3885a4a766026d4f8243792/data/parts/CustomXmlPropertiesPart.json)
declarations. No MCP delivery or native interoperability acceptance is implied.

Fictional Word for Mac 16.95.1 save/reopen checks confirmed the expected accepted and
rejected text. The accepted engine result and native Word acceptance had equal
effective formatting and identical full-page PDF renders. Word also reserialized
stored XML, populated schema references and added MC attributes even in a no-op
save. The closed root-annotation/association profile permits these particular
fictional Word-saved inputs to be inventoried and processed again, with exact
non-main-part preservation and zero-revision ZIP identity. These input replay
checks are separate from native handling. A fresh follow-up used owned copies of
the reingested engine candidate and the native accepted reference: both completed
Save As, close and reopen in Word, retained their literal accepted text and had
equal effective text/paragraph/mark properties in the declared plain-paragraph
model. Both fresh Word outputs were eligible and returned byte-identically when
already clean. Their stored payload bytes and named references survived this
particular second save. Screenshots confirmed the visible paragraph; a new
full-page PDF pixel comparison was not performed. This is a bounded fictional
roundtrip, not acceptance of a real contract or reserved Office metadata. The engine's
exact-byte preservation proof applies to its own candidate, not to subsequent
Word saves; no Word roundtrip byte-identity is claimed.

All existing ZIP/XML resource limits apply. The inventory additionally bounds
aggregate XML work, depth and findings. Invalid numbering-definition order blocks
export rather than relying on Word repair. The engine keeps the same revision
and validation-byte limits. Repeated `locked` and ligature leaves are checked
without repeatedly scanning their siblings; invalid duplicates still block.
The selective path charges the full main document for
repeated outcome validation. The batch path charges its full-source copies and
actual local validation work. Successfully proved formatting dependencies may be
reused only for the same immutable dependency bytes and reference semantics;
failed proofs are never cached. Actual dependency reads consume the same budget.
Exceeding a bound refuses
the entire candidate; it never truncates an inventory, accepts only the first
page or labels a partial result clean. It performs no filesystem operation and
returns no authorization or journal receipt.

Acceptance uses fictional fixtures with literal expected text, properties,
numbering and row/paragraph structure, plus independent native Word comparisons.
Real-pair acceptance remains separate until the user selects a sent/received
pair. File publication, outgoing-folder atomicity, comment removal and property
policies remain required work for the next stage.
