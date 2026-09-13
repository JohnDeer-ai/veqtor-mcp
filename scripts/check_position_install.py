# SPDX-License-Identifier: Apache-2.0
"""Exact development wheel/source/install binding; independent of frozen releases."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
import zipfile


class InstallError(ValueError):
    pass


def require(value, message):
    if not value:
        raise InstallError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()


def verify(root, commit, tree, wheel, sdist, python):
    root, wheel, sdist = map(lambda x: Path(x).resolve(), (root, wheel, sdist))
    python = Path(python).absolute()  # Preserve the virtualenv interpreter symlink.
    require(git(root, "rev-parse", "HEAD") == commit, "source HEAD differs")
    require(git(root, "rev-parse", "HEAD^{tree}") == tree, "source tree differs")
    require(not git(root, "status", "--porcelain=v1", "--untracked-files=all"), "source checkout is dirty")
    config = tomllib.loads((root / "pyproject.toml").read_text())
    require(config["project"]["version"] == "0.4.2.dev0", "wrong development version")
    runtime = {name.removeprefix("/src/"): (root / name.lstrip("/")).read_bytes()
               for name in config["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]}
    require(set(runtime) == {p.relative_to(root / "src").as_posix()
                            for package in ("veqtor_mcp", "veqtor_docx")
                            for p in (root / "src" / package).rglob("*.py")}, "runtime source set differs")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        dist_info = "veqtor_mcp-0.4.2.dev0.dist-info"
        generated = {f"{dist_info}/{name}" for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE", "licenses/NOTICE")}
        require(len(names) == len(set(names)), "duplicate wheel members")
        require(set(names) == set(runtime) | generated, "unexpected wheel member")
        require({n for n in names if n.startswith(("veqtor_mcp/", "veqtor_docx/"))} == set(runtime),
                "unexpected wheel package data")
        require(all(archive.read(name) == data for name, data in runtime.items()), "wheel source differs")
        require(all(".veqtor" not in Path(n).parts for n in names), "private store in wheel")
    expected_sdist = {name.lstrip("/") for name in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]}
    with tarfile.open(sdist) as archive:
        entries = [member for member in archive.getmembers() if not member.isdir()]
        members = {member.name.split("/", 1)[1]: member for member in entries}
        # Hatch adds package metadata to the explicit development selection.
        require(len(entries) == len(members), "duplicate sdist members")
        require(set(members) == expected_sdist | {"PKG-INFO"}, "sdist file selection differs")
        for name in expected_sdist:
            member = members[name]
            require(member.isfile() and archive.extractfile(member).read() == (root / name).read_bytes(),
                    "sdist source differs")
    probe = '''
import asyncio, importlib.metadata, json
from pathlib import Path
from veqtor_mcp import __version__, records, server
import veqtor_mcp, veqtor_docx
async def main():
    tools = await server.mcp.list_tools()
    print(json.dumps(dict(version=__version__, distribution_version=importlib.metadata.version('veqtor-mcp'),
        build=records.SOURCE_SNAPSHOT_IDENTITY,
        roots={m.__name__:str(Path(m.__file__).parent) for m in (veqtor_mcp,veqtor_docx)},
        tools=[t.name for t in tools])))
asyncio.run(main())
'''
    # Isolated interpreter ignores ambient Python path and current checkout.
    observed = json.loads(subprocess.check_output([str(python), "-I", "-c", probe], cwd=wheel.parent))
    require(observed["version"] == observed["distribution_version"] == config["project"]["version"],
            "installed distribution version differs")
    require(set(observed["tools"]) == {"list_rounds", "extract_redlines", "inspect_document", "map_rounds",
        "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote", "export_decision_record",
        "read_deal_positions", "mutate_deal_positions"}, "installed tool set differs")
    actual = {}
    for package, raw in observed["roots"].items():
        location = Path(raw).resolve()
        require(not location.is_relative_to(root), "installed producer comes from checkout")
        for path in location.rglob("*.py"):
            require(not path.is_symlink(), "installed symlink source")
            actual[f"{package}/{path.relative_to(location).as_posix()}"] = path.read_bytes()
    require(actual == runtime, "installed Python sources differ from wheel/source")
    manifest = {"schema": "source_snapshot.v1", "files": [
        {"path": name, "sha256": digest(data)} for name, data in sorted(runtime.items())]}
    build = "source-snapshot-v1-sha256:" + digest(json.dumps(manifest, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode())
    require(observed["build"] == build, "installed source fingerprint differs")
    return {"schema_version": "veqtor_position_install.v1", "commit": commit, "tree": tree,
            "source_root": str(root), "wheel": str(wheel), "sdist": str(sdist),
            "wheel_sha256": digest(wheel.read_bytes()), "sdist_sha256": digest(sdist.read_bytes()),
            "python": str(python), "producer": {"name": "veqtor-mcp", "version": observed["version"], "build": build},
            "source_files": {name: digest(data) for name, data in sorted(runtime.items())},
            "installed_roots": observed["roots"], "tools": observed["tools"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "commit", "tree", "wheel", "sdist", "python"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.source_root, args.commit, args.tree, args.wheel, args.sdist, args.python)
        print(json.dumps(report, sort_keys=True))
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError):
        print("position install check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
