# SPDX-License-Identifier: Apache-2.0
"""Shared source-only interpreter launch for NR-02 install and native evidence."""
import json


# -B alone still reads timestamp-valid or unchecked-hash .pyc files. Compile
# source directly before importing the producer, including runpy's __main__
# path. SourceFileLoader's normal cache lookup/write is never entered.
SOURCE_ONLY_BOOTSTRAP = """from importlib.machinery import SourceFileLoader
def _source_only_code(self, fullname):
    return self.source_to_code(self.get_data(self.path), self.path)
SourceFileLoader.get_code = _source_only_code
"""
SERVER_ARGS = ["-I", "-B", "-c", SOURCE_ONLY_BOOTSTRAP
    + "import runpy\nrunpy.run_module('veqtor_mcp.server', run_name='__main__')\n"]
SERVER_OVERRIDE = "mcp_servers.veqtor_nr02.args=" + json.dumps(SERVER_ARGS, separators=(",", ":"))
