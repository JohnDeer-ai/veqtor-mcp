# SPDX-License-Identifier: Apache-2.0
"""Preserve and restore genuine legacy rollout bytes, never fabricate a store.

Only new explicitly legacy captures qualify. Paginated state needs its own
authoritative native store lifecycle and is deliberately refused here.
"""
import hashlib
from pathlib import Path
import re

from check_codex_acceptance import _require


def companion(prefix):
    prefix = Path(prefix)
    _require(prefix.name.endswith(".session.jsonl"), "source parent prefix filename differs")
    return prefix.with_name(prefix.name.removesuffix(".session.jsonl") + ".resume.jsonl")


def regular_bytes(path):
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts and path.is_file()
             and not any(p.is_symlink() for p in (path, *path.parents)), "source persisted state missing or symlink")
    return path.read_bytes()


def state_binding(raw, prefix, relative_path, *, thread, cwd):
    from nr03_app_server import BUILD, lines
    _require(isinstance(relative_path, str) and re.fullmatch(
        r"sessions/\d{4}/\d{2}/\d{2}/rollout-[^/]+-" + re.escape(thread) + r"\.jsonl", relative_path)
        and ".." not in Path(relative_path).parts, "source persisted rollout layout or identity differs")
    rows = lines(raw)
    meta = rows[0].get("payload", {})
    _require(rows[0].get("type") == "session_meta" and meta.get("id") == thread
             and meta.get("cwd") == str(cwd) and meta.get("history_mode") == "legacy"
             and meta.get("cli_version") == BUILD["version"], "source persisted legacy identity/build differs")
    entries = raw.splitlines(keepends=True)
    ends = [i for i, r in enumerate(rows) if r.get("type") == "event_msg"
            and r.get("payload", {}).get("type") == "task_complete"]
    _require(ends and b"".join(entries[:ends[-1] + 1]) == prefix
             and not any(r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_started"
                         for r in rows[ends[-1] + 1:]), "source persisted state has stale or incomplete prefix")
    return dict(schema_version="nr03-legacy-state.v1", history_mode="legacy", thread_id=thread, cwd=str(cwd),
        relative_rollout_path=relative_path, rollout_sha256=hashlib.sha256(raw).hexdigest(), rollout_bytes=len(raw),
        prefix_sha256=hashlib.sha256(prefix).hexdigest(), prefix_bytes=len(prefix))


def validate_state(raw, prefix, value, *, thread, cwd):
    _require(isinstance(value, dict), "source persisted state binding missing")
    expected = state_binding(raw, prefix, value.get("relative_rollout_path"), thread=thread, cwd=cwd)
    _require(set(value) == set(expected) and all(type(value[k]) is type(v) and value[k] == v
             for k, v in expected.items()), "source persisted state bytes or binding differ")
    return expected


def parent_state(prefix_path, source, *, thread, cwd, selection):
    from nr03_app_server import BUILD, PROFILE, policy_hashes
    _require(isinstance(source, dict) and source.get("profile") == PROFILE and source.get("build") == BUILD
             and source.get("evidence_kind") == "native" and source.get("policy_sha256") == policy_hashes()
             and source.get("thread_id") == thread and source.get("selection") == selection,
             "source parent state is not a current qualified native capture")
    _require(isinstance(prefix_path, (str, Path)), "source parent prefix missing")
    prefix, raw = regular_bytes(prefix_path), regular_bytes(companion(prefix_path))
    _require(hashlib.sha256(prefix).hexdigest() == source.get("session_sha256"), "source parent prefix bytes differ")
    binding = validate_state(raw, prefix, source.get("persisted_state"), thread=thread, cwd=cwd)
    return raw, binding


def restore_state(runtime, raw, binding):
    from capture_position_session import private_write
    prefix = raw[:binding["prefix_bytes"]]
    validate_state(raw, prefix, binding, thread=binding["thread_id"], cwd=binding["cwd"])
    target = Path(runtime) / binding["relative_rollout_path"]
    _require(target.resolve().is_relative_to(Path(runtime).resolve())
             and not any(p.is_symlink() for p in (target, *target.parents)), "source restore path escaped runtime")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(target, raw)
    _require(regular_bytes(target) == raw, "source original parent state copy differs")
    return target


def validate_parent_prefix(raw, parent, *, thread, cwd):
    _require(isinstance(parent, dict) and type(parent.get("rollout_bytes")) is int
             and type(parent.get("prefix_bytes")) is int
             and 0 < parent["prefix_bytes"] <= parent["rollout_bytes"] <= len(raw),
             "source resumed parent state missing or incomplete")
    validate_state(raw[:parent["rollout_bytes"]], raw[:parent["prefix_bytes"]], parent, thread=thread, cwd=cwd)


def validate_ancestry(source, parent_source):
    if source.get("evidence_kind") == "native":
        _require(parent_source.get("evidence_kind") == "native"
                 and isinstance(source.get("parent_state"), dict)
                 and source["parent_state"] == parent_source.get("persisted_state"),
                 "source restored state differs from bound parent receipt")
