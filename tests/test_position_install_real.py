# SPDX-License-Identifier: Apache-2.0
"""Opt-in replay of genuine artifacts/runtime: no Git, probe or producer mocks.

Set VEQTOR_POSITION_INSTALL_FIXTURE to a private JSON file containing source_root,
commit, tree, wheel, sdist, python and lock. Inputs remain read-only; all variants
and the runtime copy are placed under pytest's external temporary directory.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest

from test_position_install_contract import ARCHIVE_CASES, INSTALLED_CASES, archive_variant, corrupt_installed, installed_target
from test_position_gate_artifacts import install


pytestmark = pytest.mark.skipif(not os.environ.get("VEQTOR_POSITION_INSTALL_FIXTURE"),
    reason="genuine artifact pair and locked isolated installation must be provided explicitly")


def inventory(root):
    return {path.relative_to(root).as_posix(): {"link": os.readlink(path)} if path.is_symlink()
        else {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in root.rglob("*")
        if path.is_symlink() or path.is_file()}


@pytest.fixture(scope="module")
def real_case(tmp_path_factory):
    manifest = Path(os.environ["VEQTOR_POSITION_INSTALL_FIXTURE"])
    config = json.loads(manifest.read_bytes())
    root = Path(config["source_root"]).resolve()
    directory = tmp_path_factory.mktemp("real-position-install")
    assert not directory.resolve().is_relative_to(root)
    original_runtime = Path(config["python"]).absolute().parent.parent
    assert (original_runtime / "pyvenv.cfg").is_file()
    original = inventory(original_runtime)
    runtime = directory / "runtime"
    shutil.copytree(original_runtime, runtime, symlinks=True)
    assert inventory(runtime) == original
    wheel, sdist = (directory / Path(config[name]).name for name in ("wheel", "sdist"))
    source_hashes = {}
    for name, target in (("wheel", wheel), ("sdist", sdist)):
        raw = Path(config[name]).read_bytes()
        target.write_bytes(raw)
        source_hashes[name] = hashlib.sha256(raw).hexdigest()
    python = runtime / "bin" / Path(config["python"]).name
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VEQTOR_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [str(python), "-B", str(root / "scripts/check_position_install.py"),
        "--source-root", str(root), "--commit", config["commit"], "--tree", config["tree"],
        "--wheel", str(wheel), "--sdist", str(sdist), "--python", str(python)]

    def canonical(label):
        result = subprocess.run(command, cwd=directory, env=env, capture_output=True, check=False)
        (directory / (label + ".stdout.json")).write_bytes(result.stdout)
        (directory / (label + ".stderr.log")).write_bytes(result.stderr)
        (directory / (label + ".result.json")).write_text(json.dumps({"argv": command, "exit_code": result.returncode,
            "cwd": str(directory), "archive_sha256": source_hashes,
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest()}, indent=2) + "\n")
        assert result.returncode == 0, result.stderr.decode()
        return json.loads(result.stdout)

    positive = canonical("genuine-positive-before")
    source_files = {p.relative_to(root / "src").as_posix(): p.read_bytes() for package in ("veqtor_mcp", "veqtor_docx")
        for p in (root / "src" / package).rglob("*.py")}
    identity = {"schema": "source_snapshot.v1", "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in sorted(source_files.items())]}
    expected_build = "source-snapshot-v1-sha256:" + hashlib.sha256(json.dumps(identity,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert positive["producer"]["build"] == expected_build
    assert positive["source_files"] == {n: hashlib.sha256(d).hexdigest() for n, d in source_files.items()}

    # Independently evaluate the lock against the actual copied interpreter.
    probe = """import importlib.metadata,json,os,platform,sys
marker_environment=dict(implementation_name=sys.implementation.name,
    implementation_version=platform.python_version(), os_name=os.name,
    platform_machine=platform.machine(), platform_python_implementation=platform.python_implementation(),
    platform_release=platform.release(), platform_system=platform.system(), platform_version=platform.version(),
    python_full_version=platform.python_version(), python_version='.'.join(platform.python_version_tuple()[:2]),
    sys_platform=sys.platform)
print(json.dumps(dict(python=platform.python_version(), marker_environment=marker_environment,
    distributions={d.metadata['Name']:d.version for d in importlib.metadata.distributions()})))"""
    observed = json.loads(subprocess.check_output([str(python), "-I", "-B", "-c", probe], cwd=directory, env=env))
    actual = {canonicalize_name(n): v for n, v in observed["distributions"].items() if canonicalize_name(n) != "veqtor-mcp"}
    marker_env = observed["marker_environment"]
    pinned = {}
    raw_lock = Path(config["lock"]).read_bytes()
    for line in raw_lock.decode().replace("\\\n", "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line.split("--hash=", 1)[0].strip())
        if requirement.marker is None or requirement.marker.evaluate(marker_env):
            versions = list(requirement.specifier)
            assert len(versions) == 1 and versions[0].operator == "=="
            pinned[canonicalize_name(requirement.name)] = versions[0].version
    assert actual == pinned
    (directory / "independent-identity.json").write_text(json.dumps({"expected_build": expected_build,
        "actual_build": positive["producer"]["build"], "locked_dependencies": pinned,
        "observed_dependencies": actual, "python": observed["python"],
        "lock_sha256": hashlib.sha256(raw_lock).hexdigest(), "copied_runtime_entries": len(original)}, indent=2) + "\n")
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {n: archive.read(n) for n in archive.namelist()}
    with tarfile.open(sdist) as archive:
        sdist_files = {m.name.split("/", 1)[1]: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
    case = SimpleNamespace(root=root, wheel=wheel, sdist=sdist, wheel_files=wheel_files, sdist_files=sdist_files,
        installed=Path(positive["installed_distribution"]).parent, dist=Path(positive["installed_distribution"]).name,
        args=(root, config["commit"], config["tree"], wheel, sdist, python), positive=positive, directory=directory)
    yield case
    assert canonical("genuine-positive-after") == positive
    assert inventory(original_runtime) == original
    assert inventory(runtime) == original
    assert all(hashlib.sha256(Path(config[n]).read_bytes()).hexdigest() == source_hashes[n] for n in ("wheel", "sdist"))
    (directory / "preservation.json").write_text(json.dumps({"original_runtime_unchanged": True,
        "copied_runtime_restored": True, "original_archives_unchanged": True,
        "runtime_entries": len(original), "archive_sha256": source_hashes}, indent=2) + "\n")


def test_genuine_artifact_install_positive_control(real_case):
    assert install.verify(*real_case.args) == real_case.positive


@pytest.mark.parametrize("mutation", ARCHIVE_CASES)
def test_genuine_artifact_corruptions_fail(real_case, tmp_path, mutation):
    wheel, sdist = archive_variant(real_case, mutation, tmp_path)
    with pytest.raises((install.InstallError, OSError)) as exc:
        install.verify(*real_case.args[:3], wheel, sdist, real_case.args[-1])
    (tmp_path / "rejection.json").write_text(json.dumps({"case": mutation, "error": str(exc.value),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest() if wheel.is_file() else None,
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest() if sdist.is_file() else None}, indent=2) + "\n")
    assert install.verify(*real_case.args) == real_case.positive


@pytest.mark.parametrize("mutation", INSTALLED_CASES)
def test_genuine_installed_corruptions_fail(real_case, tmp_path, mutation):
    path = installed_target(real_case, mutation)
    saved = path.read_bytes() if path.exists() else None
    try:
        corrupt_installed(path, mutation)
        with pytest.raises((install.InstallError, OSError, subprocess.CalledProcessError)) as exc:
            install.verify(*real_case.args)
        (tmp_path / "rejection.json").write_text(json.dumps({"case": mutation, "error": str(exc.value)}, indent=2) + "\n")
    finally:
        path.unlink(missing_ok=True)
        if saved is not None:
            path.write_bytes(saved)
        else:
            path.parent.rmdir()
    assert install.verify(*real_case.args) == real_case.positive
