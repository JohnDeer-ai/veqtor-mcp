# Decision record v1 compatibility fixtures

The JSONL journal and its compact JSON projection are static pre-promotion v1
contract fixtures. Tests must read these bytes as committed; they must never
regenerate expected values through the implementation under test.

After v1 promotion, preserve existing journal frames and expected projections.
Add new fixtures for compatible additions, and use a new schema-version fixture
for incompatible changes. Never update these files merely to make a failing
compatibility test pass.

| Frozen semantic | Discriminating coverage |
| --- | --- |
| UTF-8 and `ensure_ascii=False` | Canonical Unicode vectors and Unicode historical frame |
| Sorted keys and compact separators | Unsorted composite payload with literal canonical bytes |
| Float representation and signed zero | Composite canonical vector with `-0.0` |
| Control-character escaping | Composite vector with U+0000 and all short escapes |
| Lowercase non-short control escapes | Literal U+000B/U+000E/U+000F/U+001A/U+001B/U+001E/U+001F vector |
| Code-point rather than UTF-16 key order | Composite U+E000/U+10000 vector |
| No NFC/NFD normalization of keys or values | Literal NFC/NFD value and key vectors |
| Historical input/result digests | Raw and compact golden fixtures |
| Compact sample size 20 | Exact ratchet and 10,000-anchor projection test |
| Compact privacy | ASCII and Unicode sentinel assertions |
| Pre-bounded snapshot privacy | 25-item adversarial snapshot is reprojected and capped |
