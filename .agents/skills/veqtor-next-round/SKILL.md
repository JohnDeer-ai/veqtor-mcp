---
name: veqtor-next-round
description: Prepare a selected-issue negotiation brief and tracked Word counterproposal with local Veqtor MCP, reusing saved positions in a later round.
metadata:
  version: nr03-next-round.v1
---

# Veqtor next round

Read and follow the complete [canonical workflow](../../../docs/prompts/next-round.md).
Resolve that path relative to this skill directory; when distributing the skill,
keep the same layout with the canonical file. If unavailable, report the missing
workflow instead of reconstructing its rules. The canonical prompt is also the
portable local Claude entrypoint.

Use the user's matter folder, incoming DOCX, optional previously sent DOCX, selected
issues and new output path. Begin by reading saved positions through Veqtor; ask
only for missing decisions. The workflow contains the exact evidence, freshness,
complete-batch, refusal and readback requirements. Contract DOCX writes use Veqtor
MCP exclusively. Preserve the distinction between a stored confirmation, a pending
business decision and permission for particular wording.
