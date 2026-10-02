# SPDX-License-Identifier: Apache-2.0
"""One predeclared native acceptance fault; never part of the installed product.

The launch receipt contains this exact source. It simulates a filesystem error
once, at the actual NR-02 post-publication directory fsync, not a fake MCP result.
"""
from position_source_launch import SOURCE_ONLY_BOOTSTRAP

FAULT = "q1-v2-post-publication-directory-fsync"


def fault_args(matter):
    source = '''
import errno as _nr03_errno
import json as _nr03_json
import os as _nr03_os
import stat as _nr03_stat
import sys as _nr03_sys
_nr03_real_fsync = _nr03_os.fsync
_nr03_fault_fired = False
_nr03_meta = _nr03_os.stat(MATTER + '/.veqtor', follow_symlinks=False)
_nr03_meta_identity = (_nr03_meta.st_dev, _nr03_meta.st_ino)
def _nr03_fsync(fd):
    global _nr03_fault_fired
    frame = _nr03_sys._getframe(1)
    info = _nr03_os.fstat(fd)
    if (not _nr03_fault_fired and frame.f_code.co_name == '_publish'
            and frame.f_globals.get('__name__') == 'veqtor_mcp.positions'
            and _nr03_stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino) == _nr03_meta_identity):
        opened = _nr03_os.open('deal-positions.json', _nr03_os.O_RDONLY | _nr03_os.O_NOFOLLOW, dir_fd=fd)
        with _nr03_os.fdopen(opened, 'rb') as stream:
            snapshot = _nr03_json.load(stream)
        target = next((p for p in snapshot['positions'] if p['position_id'] == 'pos_00000000000000000000000000000001'), None)
        if (target and target['version'] == 2 and target['confirmation'] is None
                and target['content']['desired_outcome'] == 'Protect confidential information for four years after termination.'):
            _nr03_fault_fired = True
            raise OSError(_nr03_errno.EIO, 'NR-03 synthetic post-publication directory fsync fault')
    return _nr03_real_fsync(fd)
_nr03_os.fsync = _nr03_fsync
'''.replace("MATTER", repr(str(matter)))
    return ["-I", "-B", "-c", SOURCE_ONLY_BOOTSTRAP + source
            + "\nimport runpy\nrunpy.run_module('veqtor_mcp.server', run_name='__main__')\n"]
