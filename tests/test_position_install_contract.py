# SPDX-License-Identifier: Apache-2.0
"""NR02-R6 corruptions shared by synthetic units and real installed controls."""
import io
import stat
import tarfile
import zipfile

import pytest

from test_position_gate_artifacts import artifact_case as artifact_case, bundle as bundle, checker, install, wheel_record


ARCHIVE_CASES = (
    "wheel_absent", "sdist_absent", "wheel_source_missing", "wheel_source_changed",
    "wheel_extra", "wheel_private", "wheel_duplicate", "wheel_metadata_missing",
    "wheel_metadata_name", "wheel_metadata_version", "wheel_metadata_dependency", "wheel_metadata_readme",
    "wheel_entry_points", "wheel_tag", "wheel_license", "wheel_notice", "wheel_metadata_symlink",
    "record_empty", "record_hash", "record_size", "record_missing_row", "record_duplicate_row", "record_self",
    "sdist_gitignore_missing", "sdist_gitignore_empty", "sdist_gitignore_changed",
    "sdist_wrong_root", "sdist_mixed_roots", "sdist_noncanonical", "sdist_duplicate", "sdist_private",
    "sdist_source_missing", "sdist_source_changed", "sdist_pkg_missing", "sdist_pkg_empty", "sdist_pkg_wrong",
    "sdist_pkg_symlink", "sdist_pkg_hardlink", "sdist_pkg_directory", "sdist_pkg_fifo", "both_metadata_wrong",
)
INSTALLED_CASES = ("metadata_dependency", "entry_points", "license", "notice", "wheel_tag", "source_changed",
                   "metadata_missing", "metadata_symlink", "extra_private")


def archive_variant(case, mutation, directory):
    """Only private variant bytes change. Keep original controls intact."""
    wheel, sdist = case.wheel, case.sdist
    if mutation in {"wheel_absent", "sdist_absent"}:
        absent = directory / "absent"
        return (absent, sdist) if mutation == "wheel_absent" else (wheel, absent)
    if mutation.startswith(("wheel_", "record_")) or mutation == "both_metadata_wrong":
        values = dict(case.wheel_files)
        metadata = case.dist + "/METADATA"
        record = case.dist + "/RECORD"
        if mutation == "wheel_source_missing":
            del values["veqtor_mcp/__init__.py"]
        elif mutation == "wheel_source_changed":
            values["veqtor_mcp/__init__.py"] += b"\n# changed source\n"
        elif mutation == "wheel_extra":
            values["unexpected.txt"] = b"unexpected"
        elif mutation == "wheel_private":
            values["veqtor_mcp/.veqtor/deal-positions.json"] = b"{}"
        elif mutation == "wheel_metadata_missing":
            del values[metadata]
        elif mutation in {"wheel_metadata_name", "both_metadata_wrong"}:
            values[metadata] = values[metadata].replace(b"Name: veqtor-mcp", b"Name: unrelated")
        elif mutation == "wheel_metadata_version":
            values[metadata] = values[metadata].replace(b"Version: 0.4.2.dev0", b"Version: 0.0.0")
        elif mutation == "wheel_metadata_dependency":
            values[metadata] = values[metadata].replace(b"jsonschema<5,>=4.20", b"unrelated==99")
        elif mutation == "wheel_metadata_readme":
            values[metadata] += b"Injected description\n"
        elif mutation == "wheel_entry_points":
            values[case.dist + "/entry_points.txt"] = b"[console_scripts]\nveqtor-mcp = wrong.module:main\n"
        elif mutation == "wheel_tag":
            values[case.dist + "/WHEEL"] = values[case.dist + "/WHEEL"].replace(b"py3-none-any", b"cp39-none-win32")
        elif mutation in {"wheel_license", "wheel_notice"}:
            values[case.dist + "/licenses/" + mutation.removeprefix("wheel_").upper()] = b""
        # Regenerate a fully consistent RECORD for semantic corruption cases.
        # Rejection must not depend merely on leaving a stale archive hash.
        values = wheel_record(values)
        if mutation.startswith("record_"):
            rows = values[record].splitlines(keepends=True)
            if mutation == "record_empty":
                values[record] = b""
            elif mutation == "record_hash":
                fields = rows[0].split(b",")
                fields[1] = b"sha256=" + b"A" * 43
                values[record] = b",".join(fields) + b"".join(rows[1:])
            elif mutation == "record_size":
                fields = rows[0].split(b",")
                fields[2] = b"999999\n"
                values[record] = b",".join(fields) + b"".join(rows[1:])
            elif mutation == "record_missing_row":
                values[record] = b"".join(rows[1:])
            elif mutation == "record_duplicate_row":
                values[record] += rows[0]
            else:
                values[record] = values[record].replace((record + ",,\n").encode(), (record + ",sha256=wrong,1\n").encode())
        wheel = directory / (mutation + ".whl")
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in values.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16 if mutation == "wheel_metadata_symlink" and name == metadata else (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data)
            if mutation == "wheel_duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(metadata, values[metadata])
    if mutation.startswith("sdist_") or mutation == "both_metadata_wrong":
        values = dict(case.sdist_files)
        if mutation == "sdist_gitignore_missing":
            del values[".gitignore"]
        elif mutation == "sdist_gitignore_empty":
            values[".gitignore"] = b""
        elif mutation == "sdist_gitignore_changed":
            values[".gitignore"] += b"unexpected\n"
        elif mutation == "sdist_private":
            values[".veqtor/deal-positions.json"] = b"{}"
        elif mutation == "sdist_source_missing":
            del values["src/veqtor_mcp/__init__.py"]
        elif mutation == "sdist_source_changed":
            values["src/veqtor_mcp/__init__.py"] += b"\n# changed source\n"
        elif mutation == "sdist_pkg_missing":
            del values["PKG-INFO"]
        elif mutation == "sdist_pkg_empty":
            values["PKG-INFO"] = b""
        elif mutation in {"sdist_pkg_wrong", "both_metadata_wrong"}:
            values["PKG-INFO"] = values["PKG-INFO"].replace(b"Name: veqtor-mcp", b"Name: unrelated")
        sdist = directory / (mutation + ".tar.gz")
        with tarfile.open(sdist, "w:gz") as archive:
            for name, data in values.items():
                prefix = "unrelated-0.0.0" if mutation == "sdist_wrong_root" or mutation == "sdist_mixed_roots" and name == "PKG-INFO" else "veqtor_mcp-0.4.2.dev0"
                member = tarfile.TarInfo(prefix + "/" + ("./" if mutation == "sdist_noncanonical" else "") + name)
                member.size = len(data)
                if name == "PKG-INFO" and mutation in {"sdist_pkg_symlink", "sdist_pkg_hardlink", "sdist_pkg_directory", "sdist_pkg_fifo"}:
                    member.type = {"sdist_pkg_symlink": tarfile.SYMTYPE, "sdist_pkg_hardlink": tarfile.LNKTYPE,
                        "sdist_pkg_directory": tarfile.DIRTYPE, "sdist_pkg_fifo": tarfile.FIFOTYPE}[mutation]
                    member.linkname = "README.md"
                    member.size = 0
                archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
                if mutation == "sdist_duplicate" and name == ".gitignore":
                    archive.addfile(member, io.BytesIO(data))
    return wheel, sdist


def installed_target(case, mutation):
    relative = {"metadata_dependency": "METADATA", "entry_points": "entry_points.txt", "license": "licenses/LICENSE",
        "notice": "licenses/NOTICE", "wheel_tag": "WHEEL", "metadata_missing": "METADATA", "metadata_symlink": "METADATA",
        "extra_private": ".veqtor/deal-positions.json"}.get(mutation)
    return case.installed / case.dist / relative if relative else case.installed / "veqtor_mcp/__init__.py"


def corrupt_installed(path, mutation):
    if mutation == "metadata_missing":
        path.unlink()
    elif mutation == "metadata_symlink":
        path.unlink()
        path.symlink_to("WHEEL")
    elif mutation == "metadata_dependency":
        path.write_bytes(path.read_bytes().replace(b"jsonschema<5,>=4.20", b"unrelated==99"))
    elif mutation == "source_changed":
        path.write_bytes(path.read_bytes() + b"\n# changed installed source\n")
    else:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b"" if mutation in {"license", "notice"} else b"invalid identity\n")


@pytest.mark.parametrize("mutation", ARCHIVE_CASES)
def test_archive_content_contract(artifact_case, tmp_path, mutation):
    case = artifact_case
    assert install.verify(*case.args)["producer"]["build"] == case.build_id
    wheel, sdist = archive_variant(case, mutation, tmp_path)
    with pytest.raises((install.InstallError, OSError)):
        install.verify(*case.args[:3], wheel, sdist, case.args[-1])
    assert install.verify(*case.args)["producer"]["build"] == case.build_id


@pytest.mark.parametrize("mutation", INSTALLED_CASES)
def test_installed_distribution_content_contract(artifact_case, mutation):
    case = artifact_case
    positive = install.verify(*case.args)
    path = installed_target(case, mutation)
    saved = path.read_bytes() if path.exists() else None
    try:
        corrupt_installed(path, mutation)
        with pytest.raises((install.InstallError, OSError)):
            install.verify(*case.args)
    finally:
        path.unlink(missing_ok=True)
        if saved is not None:
            path.write_bytes(saved)
        else:
            path.parent.rmdir()
    assert install.verify(*case.args) == positive


@pytest.mark.parametrize("change", [{"tools": ["read_deal_positions"] * 11}, {"build": "wrong-build"},
    {"prefix": "/outside/unrelated"}, {"distribution_path": "/outside/unrelated.dist-info"}])
def test_weaker_probe_identity_is_rejected(artifact_case, change):
    artifact_case.probe.update(change)
    with pytest.raises(install.InstallError):
        install.verify(*artifact_case.args)


def test_final_bundle_revalidates_installation_content(bundle, monkeypatch):
    def refused(*args):
        raise install.InstallError("installed distribution content differs from wheel")
    monkeypatch.setattr(install, "verify", refused)
    with pytest.raises(install.InstallError, match="distribution content"):
        checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")
