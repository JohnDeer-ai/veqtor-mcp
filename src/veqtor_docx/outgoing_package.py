# SPDX-License-Identifier: Apache-2.0
"""Internal F8 foundation: exact redline/checked clean candidates, one new folder.

All comments and properties are preserved. No ready-to-send, actual-send,
legal-finalization or metadata-cleanup claim is made. The caller must persist
report bytes outside the outgoing folder and implement the user-facing policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import unicodedata
import uuid

from . import round_snapshots as s
from ._drafting_io import (
    _identity, _rename_noreplace, _supported_filesystem, _version, _write_all,
    require_editable_path,
)
from ._ooxml import DocxError, MAX_DOCX_INPUT_BYTES
from .clean_copy import prepare_clean_copy
from .revision_inventory import inventory_package_revisions

MAX_REPORT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class OutgoingFile:
    kind: str
    name: str
    payload: bytes


@dataclass(frozen=True)
class PreparedOutgoing:
    source: s.DocumentSnapshot
    files: tuple[OutgoingFile, ...]
    report: bytes
    plan_sha256: str


def _fail(code, detail, **metadata):
    raise s.SnapshotError(code, detail, **metadata)


def _name(value):
    if (type(value) is not str or not 6 <= len(value) <= 120
            or len(value.encode('utf-8')) > 240 or unicodedata.normalize('NFC', value) != value
            or not value.endswith('.docx') or not value[0].isalnum()
            or any(not (c.isalnum() or c in ' -_()') for c in value[:-5])
            or value[:-5].rstrip() != value[:-5]
            or value[:-5].casefold() in {'con', 'prn', 'aux', 'nul',
                *('com' + str(n) for n in range(10)), *('lpt' + str(n) for n in range(10))}):
        _fail('invalid_outgoing_name', 'choose a plain portable DOCX filename')
    return value


def prepare_outgoing_package(
    source: s.DocumentSnapshot, destination: str, *, redline_name: str | None = None,
    clean_draft_name: str | None = None,
) -> PreparedOutgoing:
    """Prepare the whole selected kit in memory; an unsupported member refuses all.

    Choosing clean_draft_name explicitly requests accept-all on a separate copy.
    The result is a current-reading draft, never evidence of legal agreement.
    """
    if not isinstance(source, s.DocumentSnapshot):
        _fail('invalid_snapshot', 'capture an exact source snapshot first')
    s.DocumentSnapshot(source.payload, source.sha256, source.size_bytes)
    names = [n for n in (redline_name, clean_draft_name) if n is not None]
    if not names or len({_name(n).casefold() for n in names}) != len(names):
        _fail('invalid_outgoing_plan', 'select distinct filenames for at least one output')
    output = s._path(destination)
    require_editable_path(output)
    s._validate(source.payload)
    files = []
    inventory = inventory_package_revisions(source.payload)
    clean = None
    if redline_name is not None:
        files.append(OutgoingFile('cumulative_redline', redline_name, source.payload))
    if clean_draft_name is not None:
        result = prepare_clean_copy(source.payload, expected_source_sha256=source.sha256)
        files.append(OutgoingFile('clean_draft', clean_draft_name, result.payload))
        clean = dict(accepted_revision_count=result.accepted_revision_count,
                     decisions=list(result.decisions), inventory_after=result.inventory_after)
    report = json.dumps(dict(
        schema_version='outgoing_preparation.v1', source_sha256=source.sha256, output_path=output,
        source_size_bytes=source.size_bytes, readiness='prepared_unreviewed',
        comments_policy='preserve_all', properties_policy='preserve_all',
        files=[dict(kind=f.kind, name=f.name, sha256=s._sha(f.payload), size_bytes=len(f.payload)) for f in files],
        inventory_before=inventory, clean_draft=clean,
        limitations=['not_a_sent_record', 'not_legal_finalization', 'not_confidentiality_clearance',
                     'comment_and_property_cleanup_not_implemented', 'word_visual_acceptance_not_performed'],
    ), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(report) > MAX_REPORT_BYTES:
        _fail('resource_limit_exceeded', 'outgoing internal report exceeds its bound')
    return PreparedOutgoing(source, tuple(files), report, hashlib.sha256(report).hexdigest())


def _validated(plan, destination, expected_plan_sha256):
    if (not isinstance(plan, PreparedOutgoing) or not s._digest(expected_plan_sha256)
            or type(plan.files) is not tuple or not 1 <= len(plan.files) <= 2
            or any(not isinstance(f, OutgoingFile) for f in plan.files)):
        _fail('invalid_outgoing_plan', 'a complete prepared outgoing plan is required')
    # Public dataclass construction/replacement cannot turn arbitrary bytes into
    # a checked clean candidate or weaken the selected preservation policy.
    names = {f.kind: f.name for f in plan.files}
    if len(names) != len(plan.files) or not names.keys() <= {'cumulative_redline', 'clean_draft'}:
        _fail('invalid_outgoing_plan', 'unsupported output kinds')
    checked = prepare_outgoing_package(plan.source, destination, redline_name=names.get('cumulative_redline'),
                                       clean_draft_name=names.get('clean_draft'))
    if plan != checked or checked.plan_sha256 != expected_plan_sha256:
        _fail('outgoing_plan_mismatch', 'review the exact current outgoing plan before publication')
    return checked


def _check_directory(fd, plan):
    s._private(fd, directory=True)
    before = _version(os.fstat(fd))
    names = {f.name for f in plan.files}
    if set(os.listdir(fd)) != names:
        _fail('outgoing_mismatch', 'outgoing folder has missing or additional files')
    for _ in range(2):
        for f in plan.files:
            if s._read_file(fd, f.name, limit=MAX_DOCX_INPUT_BYTES, private=True) != f.payload:
                _fail('outgoing_mismatch', 'outgoing bytes differ from the selected plan')
    if set(os.listdir(fd)) != names or _version(os.fstat(fd)) != before:
        _fail('source_changed', 'outgoing folder changed during verification')


def verify_outgoing_package(plan, destination, *, expected_plan_sha256):
    """Observe an existing kit after an uncertain result; never repair or delete."""
    plan = _validated(plan, destination, expected_plan_sha256)
    output = s._path(destination)
    require_editable_path(output)
    try:
        with s._directory(output) as directory:
            _supported_filesystem(directory.descriptor, directory.chain[-1][0])
            _check_directory(directory.descriptor, plan)
            directory.check()
    except OSError as exc:
        raise s.SnapshotError('outgoing_unreadable', 'cannot verify the outgoing folder') from exc
    return dict(status='matched', output_path=output, plan_sha256=plan.plan_sha256,
                legal_readiness='not_asserted', actual_send='not_asserted')


def publish_outgoing_package(plan, destination, *, expected_plan_sha256):
    """Publish only selected DOCX files as one new private directory, never overwrite.

    Save plan.report separately before calling. The caller owns that persistence;
    this internal engine never writes reports, snapshots or journals to the kit.
    On late failure verify the destination against the same frozen plan. A
    matching preexisting folder is not silently treated as a new publication.
    """
    plan = _validated(plan, destination, expected_plan_sha256)
    output = s._path(destination)
    require_editable_path(output)
    folder, name = os.path.split(output)
    stage = '.veqtor-outgoing-' + uuid.uuid4().hex + '.tmp'
    published = False
    try:
        with s._directory(folder) as parent:
            fd = parent.descriptor
            s._parent_controlled(fd)
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail('output_exists', 'outgoing destination already exists')
            os.mkdir(stage, 0o700, dir_fd=fd)
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | s._NOFOLLOW | s._CLOEXEC, dir_fd=fd)
            identity = _identity(os.fstat(stage_fd))
            children = {}
            try:
                s._private(stage_fd, directory=True)
                _supported_filesystem(stage_fd, fd)
                for f in plan.files:
                    child = os.open(f.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | s._NOFOLLOW | s._CLOEXEC,
                                    0o600, dir_fd=stage_fd)
                    try:
                        children[f.name] = _identity(os.fstat(child))
                        s._private(child, directory=False)
                        _write_all(child, f.payload)
                        os.fsync(child)
                    finally:
                        os.close(child)
                _check_directory(stage_fd, plan)
                os.fsync(stage_fd)
                parent.check()
                s._parent_controlled(fd)
                if not s._matches(fd, stage, identity):
                    _fail('source_changed', 'outgoing stage moved before publication')
                try:
                    _rename_noreplace(fd, stage, name)
                    published = True
                    os.fsync(fd)
                    parent.check()
                    s._parent_controlled(fd)
                    if not s._matches(fd, name, identity):
                        _fail('source_changed', 'published outgoing folder moved')
                    _check_directory(stage_fd, plan)
                except (OSError, DocxError) as exc:
                    completed = (True if published or s._matches(fd, name, identity)
                                 else False if s._matches(fd, stage, identity) else None)
                    published = completed
                    try:
                        parent.check()
                        _check_directory(stage_fd, plan)
                        verified = s._matches(fd, name, identity)
                    except (OSError, DocxError):
                        verified = False
                    raise s.SnapshotError(
                        'publication_uncertain' if completed is not False
                        else 'output_exists' if isinstance(exc, FileExistsError) else 'outgoing_write_failed',
                        'outgoing publication has no confirmed completion', publication_completed=completed,
                        output_path=output, output_path_verified=verified, plan_sha256=plan.plan_sha256,
                    ) from exc
            finally:
                s._cleanup(fd, stage, stage_fd, identity, children)
                os.close(stage_fd)
    except s.SnapshotError as exc:
        exc.metadata.setdefault('publication_completed', published)
        exc.metadata.setdefault('output_path', output)
        exc.metadata.setdefault('output_path_verified', False)
        raise
    except (OSError, DocxError) as exc:
        raise s.SnapshotError('publication_uncertain' if published is not False else 'outgoing_write_failed',
                            'cannot publish the complete outgoing kit safely', publication_completed=published,
                            output_path=output, output_path_verified=False) from exc
    return dict(status='published', output_path=output, plan_sha256=plan.plan_sha256,
                legal_readiness='not_asserted', actual_send='not_asserted')
