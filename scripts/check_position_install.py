# SPDX-License-Identifier: Apache-2.0
"""Exact development wheel/source/install binding; independent of frozen releases."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile

# These two helpers derive content from the supplied project/README and member
# bytes. They do not invoke the frozen release identity or its inventories.
from check_release_artifacts import _metadata_contract, _verify_wheel_record


CORE_DISTRIBUTION_FILES = ("METADATA", "WHEEL", "entry_points.txt", "licenses/LICENSE", "licenses/NOTICE")
# Installers regenerate RECORD and add their own provenance/cache bookkeeping.
# None of these bytes substitutes for source, metadata, entry-point or licence
# identity. Their contents are not required to equal bytes from the wheel.
INSTALLER_BOOKKEEPING = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}


class InstallError(ValueError):
    pass


def require(value, message):
    if not value:
        raise InstallError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()


def source_file(root, name):
    path = root / name
    require(PurePosixPath(name).as_posix() == name and not Path(name).is_absolute()
            and ".." not in Path(name).parts, "noncanonical source path")
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)),
            "source file is not regular")
    return path.read_bytes()


def generated_content(config):
    require(config["build-system"] == {"requires": ["hatchling==1.31.0"], "build-backend": "hatchling.build"},
            "unsupported development build backend")
    scripts = config["project"]["scripts"]
    entries = "[console_scripts]\n" + "".join(f"{name} = {target}\n" for name, target in sorted(scripts.items()))
    wheel = "Wheel-Version: 1.0\nGenerator: hatchling 1.31.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    return entries.encode(), wheel.encode()


def check_archives(root, config, runtime, wheel, sdist):
    project = config["project"]
    package = project["name"].replace("-", "_") + "-" + project["version"]
    dist_info = package + ".dist-info"
    entry_points, wheel_identity = generated_content(config)
    readme = source_file(root, project["readme"])
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        generated = {f"{dist_info}/{name}" for name in (*CORE_DISTRIBUTION_FILES, "RECORD")}
        require(len(names) == len(set(names)), "duplicate wheel members")
        require(set(names) == set(runtime) | generated, "unexpected wheel member")
        require(all(not info.is_dir() and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                    and not info.flag_bits & 1 for info in infos), "nonregular or encrypted wheel member")
        members = {name: archive.read(name) for name in names}
    require(all(members[name] == data for name, data in runtime.items()), "wheel source differs")
    require(members[f"{dist_info}/entry_points.txt"] == entry_points, "wheel entry points differ")
    require(members[f"{dist_info}/WHEEL"] == wheel_identity, "wheel identity differs")
    for name in ("LICENSE", "NOTICE"):
        require(members[f"{dist_info}/licenses/{name}"] == source_file(root, name), "wheel licence differs")
    try:
        _metadata_contract(members[f"{dist_info}/METADATA"], config, readme, "development wheel")
        _verify_wheel_record(members)
    except SystemExit as exc:
        raise InstallError(str(exc)) from exc

    # The pinned backend force-includes the tracked VCS exclusion file. Its
    # presence AND exact source bytes are required, not an optional exception.
    require(git(root, "ls-files", "--error-unmatch", "--", ".gitignore") == ".gitignore", "untracked .gitignore")
    expected_sdist = {name.lstrip("/") for name in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]} | {".gitignore"}
    files = {f"{package}/{name}" for name in expected_sdist | {"PKG-INFO"}}
    directories = {parent.as_posix() for name in files for parent in PurePosixPath(name).parents
                   if parent.as_posix() != "."}
    with tarfile.open(sdist) as archive:
        entries = archive.getmembers()
        names = [member.name.rstrip("/") if member.isdir() else member.name for member in entries]
        require(len(names) == len(set(names)), "duplicate sdist members")
        require(all(PurePosixPath(name).as_posix() == name and not name.startswith("/")
                    and ".." not in PurePosixPath(name).parts for name in names), "noncanonical sdist member")
        require(all(name in directories if member.isdir() else name in files
                    for name, member in zip(names, entries)), "sdist root or file selection differs")
        by_name = {name: member for name, member in zip(names, entries) if not member.isdir()}
        require(set(by_name) == files, "sdist file selection differs")
        require(all(member.isfile() and not member.issparse() for member in by_name.values()), "nonregular sdist member")
        payloads = {name: archive.extractfile(member).read() for name, member in by_name.items()}
    for name in expected_sdist:
        require(payloads[f"{package}/{name}"] == source_file(root, name), "sdist source differs")
    pkg_info = payloads[f"{package}/PKG-INFO"]
    try:
        _metadata_contract(pkg_info, config, readme, "development sdist")
    except SystemExit as exc:
        raise InstallError(str(exc)) from exc
    require(pkg_info == members[f"{dist_info}/METADATA"], "wheel/sdist metadata differs")
    return dist_info, {name: members[f"{dist_info}/{name}"] for name in CORE_DISTRIBUTION_FILES}


def installed_distribution(observed, root, python, dist_info, expected):
    prefix = Path(observed["prefix"]).resolve()
    require(Path(observed["executable"]).absolute() == python and prefix == python.parent.parent.resolve()
            and prefix != Path(observed["base_prefix"]).resolve() and not prefix.is_relative_to(root),
            "installed interpreter is not an isolated outside-checkout environment")
    location = Path(observed["distribution_path"])
    require(not location.is_symlink() and location.is_dir() and location.name == dist_info
            and location.resolve().is_relative_to(prefix), "installed distribution location differs")
    present = set()
    for path in location.rglob("*"):
        name = path.relative_to(location).as_posix()
        require(not path.is_symlink(), "installed distribution symlink")
        if path.is_dir():
            require(name == "licenses", "unexpected installed distribution directory")
        else:
            require(path.is_file(), "nonregular installed distribution member")
            present.add(name)
    require(set(expected) | {"RECORD"} <= present <= set(expected) | INSTALLER_BOOKKEEPING,
            "installed distribution file selection differs")
    require(all((location / name).read_bytes() == data for name, data in expected.items()),
            "installed distribution content differs from wheel")
    require(set(observed["roots"]) == {"veqtor_mcp", "veqtor_docx"}, "installed package roots differ")
    require(all(not Path(raw).is_symlink() and Path(raw).resolve().parent == location.resolve().parent
                for raw in observed["roots"].values()), "installed package/distribution locations differ")
    return str(location.resolve())


def verify(root, commit, tree, wheel, sdist, python):
    root, wheel, sdist = map(lambda x: Path(x).resolve(), (root, wheel, sdist))
    python = Path(python).absolute()  # Preserve the virtualenv interpreter symlink.
    require(git(root, "rev-parse", "HEAD") == commit, "source HEAD differs")
    require(git(root, "rev-parse", "HEAD^{tree}") == tree, "source tree differs")
    require(not git(root, "status", "--porcelain=v1", "--untracked-files=all"), "source checkout is dirty")
    config = tomllib.loads((root / "pyproject.toml").read_text())
    require(config["project"]["name"] == "veqtor-mcp" and config["project"]["version"] == "0.4.2.dev0", "wrong development identity")
    runtime = {name.removeprefix("/src/"): source_file(root, name.lstrip("/"))
               for name in config["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]}
    require(set(runtime) == {p.relative_to(root / "src").as_posix()
                            for package in ("veqtor_mcp", "veqtor_docx")
                            for p in (root / "src" / package).rglob("*.py")}, "runtime source set differs")
    dist_info, distribution_files = check_archives(root, config, runtime, wheel, sdist)
    probe = '''
import asyncio, importlib.metadata, json, sys
from pathlib import Path
from veqtor_mcp import __version__, records, server
import veqtor_mcp, veqtor_docx
async def main():
    tools = await server.mcp.list_tools()
    distribution = importlib.metadata.distribution('veqtor-mcp')
    print(json.dumps(dict(version=__version__, distribution_version=importlib.metadata.version('veqtor-mcp'),
        prefix=sys.prefix, base_prefix=sys.base_prefix, executable=sys.executable,
        distribution_path=str(distribution._path),
        build=records.SOURCE_SNAPSHOT_IDENTITY,
        roots={m.__name__:str(Path(m.__file__).parent) for m in (veqtor_mcp,veqtor_docx)},
        tools=[t.name for t in tools])))
asyncio.run(main())
'''
    # Isolated interpreter ignores ambient Python path and current checkout.
    observed = json.loads(subprocess.check_output([str(python), "-I", "-B", "-c", probe], cwd=wheel.parent))
    require(observed["version"] == observed["distribution_version"] == config["project"]["version"],
            "installed distribution version differs")
    require(len(observed["tools"]) == 11 and set(observed["tools"]) == {"list_rounds", "extract_redlines", "inspect_document", "map_rounds",
        "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote", "export_decision_record",
        "read_deal_positions", "mutate_deal_positions"}, "installed tool set differs")
    distribution_path = installed_distribution(observed, root, python, dist_info, distribution_files)
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
            "installed_distribution": distribution_path,
            "distribution_files": {name: digest(data) for name, data in distribution_files.items()},
            "installed_roots": observed["roots"], "tools": observed["tools"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "commit", "tree", "wheel", "sdist", "python"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.source_root, args.commit, args.tree, args.wheel, args.sdist, args.python)
        print(json.dumps(report, sort_keys=True))
    except (InstallError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError):
        print("position install check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
