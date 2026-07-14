# SPDX-License-Identifier: Apache-2.0
"""Verify public PyPI bytes and Trusted Publisher provenance fail closed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from release_contract import PROJECT_NAME, VERSION as CONTRACT_VERSION  # noqa: E402


TRUSTED_REPOSITORY = "JohnDeer-ai/veqtor-mcp"
TRUSTED_WORKFLOW = "release.yml"
TRUSTED_ENVIRONMENT = "pypi"
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)


class PyPIVerificationError(RuntimeError):
    """The public PyPI release differs from the approved distribution."""


JsonGetter = Callable[[str], dict]
Downloader = Callable[[str], bytes]


def _retryable_http_status(status: int) -> bool:
    return status in {404, 408, 429} or 500 <= status <= 599


def _request_bytes(url: str, headers: dict[str, str], failure: str) -> bytes:
    request = Request(url, headers=headers)
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        error: OSError | None = None
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                return response.read()
        except HTTPError as exc:
            if not _retryable_http_status(exc.code):
                raise PyPIVerificationError(failure) from exc
            error = exc
        except OSError as exc:
            error = exc

        if attempt == len(RETRY_DELAYS_SECONDS):
            raise PyPIVerificationError(failure) from error
        time.sleep(RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("bounded PyPI request loop did not terminate")


def _public_bytes(url: str) -> bytes:
    return _request_bytes(
        url,
        {"User-Agent": "veqtor-pypi-release-verifier"},
        "public PyPI download failed",
    )


def _public_json(url: str) -> dict:
    accept = (
        "application/vnd.pypi.integrity.v1+json"
        if "/integrity/" in url
        else "application/json"
    )
    raw = _request_bytes(
        url,
        {
            "Accept": accept,
            "User-Agent": "veqtor-pypi-release-verifier",
        },
        "public PyPI API lookup failed",
    )
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PyPIVerificationError("public PyPI API lookup failed") from exc
    if not isinstance(payload, dict):
        raise PyPIVerificationError("public PyPI API returned a non-object")
    return payload


def _local_artifacts(directory: Path) -> dict[str, bytes]:
    paths = [*directory.glob("*.whl"), *directory.glob("*.tar.gz")]
    if (
        len(paths) != 2
        or len({path.name for path in paths}) != 2
        or any(not path.is_file() for path in paths)
    ):
        raise PyPIVerificationError("expected exactly one wheel and one sdist")
    return {path.name: path.read_bytes() for path in paths}


def _package_type(filename: str) -> str:
    if filename.endswith(".whl"):
        return "bdist_wheel"
    if filename.endswith(".tar.gz"):
        return "sdist"
    raise PyPIVerificationError("PyPI returned an unexpected distribution type")


def _verify_provenance(payload: dict) -> None:
    if payload.get("version") != 1:
        raise PyPIVerificationError("PyPI provenance has an unsupported version")
    bundles = payload.get("attestation_bundles")
    if not isinstance(bundles, list):
        raise PyPIVerificationError("PyPI provenance has no attestation bundles")
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        publisher = bundle.get("publisher")
        attestations = bundle.get("attestations")
        if (
            isinstance(publisher, dict)
            and publisher.get("kind") == "GitHub"
            and publisher.get("repository") == TRUSTED_REPOSITORY
            and publisher.get("workflow") == TRUSTED_WORKFLOW
            and publisher.get("environment") == TRUSTED_ENVIRONMENT
            and isinstance(attestations, list)
            and any(
                isinstance(attestation, dict)
                and attestation.get("version") == 1
                for attestation in attestations
            )
        ):
            return
    raise PyPIVerificationError("PyPI provenance is not from the trusted workflow")


def verify(
    *,
    project: str,
    version: str,
    dist_dir: Path,
    get_json: JsonGetter = _public_json,
    download: Downloader = _public_bytes,
) -> None:
    if project != PROJECT_NAME or version != CONTRACT_VERSION:
        raise PyPIVerificationError("PyPI identity differs from release contract")

    local = _local_artifacts(dist_dir)
    project_path = quote(project, safe="")
    version_path = quote(version, safe="")
    metadata_url = f"https://pypi.org/pypi/{project_path}/{version_path}/json"
    payload = get_json(metadata_url)
    info = payload.get("info")
    urls = payload.get("urls")
    if (
        not isinstance(info, dict)
        or info.get("name") != project
        or info.get("version") != version
        or not isinstance(urls, list)
    ):
        raise PyPIVerificationError("public PyPI metadata differs from contract")

    observed: dict[str, dict] = {}
    for item in urls:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise PyPIVerificationError("public PyPI file metadata is malformed")
        filename = item["filename"]
        if filename in observed:
            raise PyPIVerificationError("public PyPI filenames are duplicated")
        observed[filename] = item
    if set(observed) != set(local):
        raise PyPIVerificationError("public PyPI file set differs from CI")

    for filename, expected_bytes in local.items():
        item = observed[filename]
        digest = hashlib.sha256(expected_bytes).hexdigest()
        digests = item.get("digests")
        url = item.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (
            item.get("packagetype") != _package_type(filename)
            or item.get("yanked") is not False
            or item.get("size") != len(expected_bytes)
            or not isinstance(digests, dict)
            or digests.get("sha256") != digest
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org"
            or unquote(Path(parsed.path).name) != filename
            or parsed.query
            or parsed.fragment
        ):
            raise PyPIVerificationError("public PyPI file metadata differs from CI")
        if download(url) != expected_bytes:
            raise PyPIVerificationError("public PyPI file bytes differ from CI")

        provenance_url = (
            "https://pypi.org/integrity/"
            f"{project_path}/{version_path}/{quote(filename, safe='')}/provenance"
        )
        _verify_provenance(get_json(provenance_url))


def main() -> int:
    try:
        verify(
            project=os.environ.get("PYPI_PROJECT", PROJECT_NAME),
            version=os.environ.get("VERSION", ""),
            dist_dir=Path(os.environ.get("DIST_DIR", "dist")),
        )
    except (OSError, PyPIVerificationError) as exc:
        print(f"PyPI release verification failed: {exc}", file=sys.stderr)
        return 1
    print("PyPI release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
