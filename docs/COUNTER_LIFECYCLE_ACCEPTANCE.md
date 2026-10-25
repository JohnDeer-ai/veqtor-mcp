<!-- SPDX-License-Identifier: Apache-2.0 -->
# Counter lifecycle candidate evidence

25 October 2026; development source `0.6.0.dev1`. This closes the implementation
gap for suffix/whole counter-insertions; it is not real-corpus or full-MVP acceptance.
The API and exact selection rules are in [REVISION_DECISIONS.md](REVISION_DECISIONS.md).

The lawyer can explicitly choose both the original insertion and the counter
decision, save, reread, make the next tracked proposal and prepare a clean
candidate. A following insertion is selected separately: it is never inferred
to be a replacement merely because it is adjacent. Unknown nested forms retain
an explicit refusal. This adds no author-based acceptance or hidden agreement.

| Evidence | Scope and result |
| --- | --- |
| Literal source matrix | Suffix/whole replacement and deletion, all four choices, with/without Word-style spelling hints; exact reading and retained neighbors/authors/auxiliary parts |
| Context controls | Fixed bilingual table cells, unselected same-paragraph comment, run formatting, stale/overlapping references, transport pagination and journal recovery |
| Negative controls | Middle/extra nesting, unknown markers/attributes, anchors between selected members, missing explicit choice, bounded reader work; refused source remains unchanged |
| Native Word reference | Six fictional copies: suffix/whole Save As + close/reopen, native all-accept and all-reject. All-accept matches current proposal; all-reject removes the original insertion too |
| Engine after Word | Eight explicitly selected combinations over the two Word-saved/reopened sources; exact literal outcomes, zero selected pending history, identical non-main part bytes |
| Following round | Both Word-saved shapes decided, edited again through fresh paragraph references, journaled and prepared as clean candidates; Word opens the expected next-round text |

Word-save compatibility required one exact document-default ligature leaf and
known empty spelling hints. The default leaf has its own canonical-part,
binding, ancestry, singleton, compatibility-declaration and finite-value proof;
it does not broaden other Office/style/default/history admission. All unknown
data remains in the source, and unsupported sources remain blocked. Auxiliary
bytes are preserved exactly during decisions and clean preparation.

Independent review found and drove fixes for an adjacent unrelated insertion
being included in the proposed group and for unknown attributes being discarded
by a newly supported compound. Regression controls cover both cases. Final
head review, complete suite, installed-package checks and required CI are
separate merge gates; their results belong to the PR and private receipts.

Remaining MVP work: outgoing package publication, issue/operation/export links
in deal state, the unified first/next-round workflow and confirmed finalization.
Real acceptance still needs user-selected permitted copies of exact sent/received
pairs for a bilingual table contract and an ordinary paragraph contract, expected
decisions/refusals, and later the lawyer's final-version confirmation. No working
contract or live client configuration was changed for these controls.
