---
name: veqtor-negotiation-round
description: Run a first or later contract negotiation round with local Veqtor MCP, saved deal state, checked edits and a selected outgoing file set. Use when preparing or continuing a contract response; does not send files or certify final agreement.
metadata:
  version: mvp-negotiation-round.v1
---

# Veqtor negotiation round

Read the complete [canonical workflow](../../../docs/prompts/negotiation-round.md).
Resolve the link relative to this directory and keep that layout when copying
the skill. If already delivered in full, use that content; if unavailable,
report the missing workflow instead of reconstructing it. This development
entrypoint requires the connected deal-state, drafting and outgoing commands;
a checkout does not upgrade an installed client.

Use the selected matter folder and documents. Start with saved state and original
positions/history, then follow the first-round or continuation route. Ask only
for missing file roles or concrete decisions. Preserve originals and already
sufficient authorization. Keep proposed wording, lawyer decisions, applied
operations, prepared files and actually sent bytes distinct.

Contract editing uses the checked Veqtor MCP protocol. Report unavailable copy
restoration, unsupported edits/export and incomplete checks without bypassing
them. The older `veqtor-next-round` skill and NR-03 evidence cover selected issues;
they do not establish acceptance of this complete-round workflow.
