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

Every mutation must equal its planned local patch. Reversing that patch in an
independent document copy must restore the exact previous XML. The final package
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
metadata are recognized without deletion; arbitrary custom XML remains blocked.
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

All existing ZIP/XML resource limits apply. The inventory additionally bounds
aggregate XML work, depth and findings. Invalid numbering-definition order blocks
export rather than relying on Word repair. The engine bounds revisions, charges
the full main document for repeated outcome validation and charges each actual
read of its formatting dependencies against the same work budget. Exceeding a bound refuses
the entire candidate; it never truncates an inventory, accepts only the first
page or labels a partial result clean. It performs no filesystem operation and
returns no authorization or journal receipt.

Acceptance uses fictional fixtures with literal expected text, properties,
numbering and row/paragraph structure, plus independent native Word comparisons.
Real-pair acceptance remains separate until the user selects a sent/received
pair. File publication, outgoing-folder atomicity, comment removal and property
policies remain required work for the next stage.
