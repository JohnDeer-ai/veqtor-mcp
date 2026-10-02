# SPDX-License-Identifier: Apache-2.0
"""Exercise saved positions through an already connected installed MCP client.

This is a scripted packaging smoke, not native model workflow acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile


async def exercise_positions(session) -> dict:
    async def call(name, arguments):
        result = await session.call_tool(name, arguments)
        data = result.structured_content
        if not isinstance(data, dict):
            data = json.loads(result.content[0].text)
        return data.get("result", data)

    with tempfile.TemporaryDirectory(prefix="veqtor-positions-smoke-") as root:
        folder = Path(root).resolve()
        root = str(folder)
        empty = await call("read_deal_positions", {"folder": root})
        assert empty["state"] == "uninitialized"
        assert empty["revision"] is None
        assert list(folder.iterdir()) == []
        position_id = "pos_" + "1" * 32
        content = {
            "title": "Synthetic payment position",
            "desired_outcome": "Payment within 30 days.",
            "fallback": None,
            "fallback_conditions": None,
            "rationale": None,
            "related_position_ids": [],
            "content_origin": "model_proposal",
            "business_decision": "not_required",
            "sources": [],
        }

        async def mutate(revision, operation):
            return await call(
                "mutate_deal_positions",
                {
                    "folder": root,
                    "expected_revision": revision,
                    "operations": [operation],
                },
            )

        saved = await mutate(
            None,
            {
                "op": "create",
                "position_id": position_id,
                "content": content,
            },
        )
        assert saved["status"] == "ok"
        assert saved["positions"][0]["confirmation"] is None
        confirmed = await mutate(
            saved["revision"],
            {
                "op": "confirm",
                "position_id": position_id,
                "expected_version": 1,
                "user_confirmed": True,
                "statement": "Synthetic test instruction: confirm this exact version 1.",
            },
        )
        assert confirmed["positions"][0]["confirmation"]["version"] == 1
        updated = await mutate(
            confirmed["revision"],
            {
                "op": "update",
                "position_id": position_id,
                "expected_version": 1,
                "content": {**content, "desired_outcome": "Payment within 45 days."},
            },
        )
        assert updated["positions"][0]["version"] == 2
        assert updated["positions"][0]["confirmation"] is None
        withdrawn = await mutate(
            updated["revision"],
            {
                "op": "withdraw",
                "position_id": position_id,
                "expected_version": 2,
            },
        )
        recovered = await call(
            "read_deal_positions",
            {
                "folder": root,
                "include_history": True,
                "check_sources": True,
            },
        )
        assert recovered["positions"] == withdrawn["positions"]
        assert recovered["revision"] == withdrawn["revision"]
        assert len(recovered["history"]) == 4
        assert recovered["history"][1]["position"]["confirmation"]["version"] == 1
        assert recovered["history"][2]["position"]["confirmation"] is None
        return {
            "status": "passed",
            "history_count": 4,
            "confirmation_reset_verified": True,
            "runtime_version": recovered["producer"]["version"],
            "runtime_producer_build": recovered["producer"]["build"],
        }
