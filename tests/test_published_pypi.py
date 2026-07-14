# SPDX-License-Identifier: Apache-2.0
"""Consumer-view verification of PyPI bytes and publisher provenance."""

from __future__ import annotations

import hashlib
import io
import importlib.util
from pathlib import Path
from urllib.error import HTTPError

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "verify_published_pypi.py"
SPEC = importlib.util.spec_from_file_location("verify_published_pypi", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
pypi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pypi)

VERSION = pypi.CONTRACT_VERSION
PROJECT = pypi.PROJECT_NAME


def _http_error(status: int) -> HTTPError:
    return HTTPError("https://pypi.org/test", status, "test", None, None)


def _fixture(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / f"veqtor_mcp-{VERSION}-py3-none-any.whl"
    sdist = dist / f"veqtor_mcp-{VERSION}.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    payloads = {path.name: path.read_bytes() for path in (wheel, sdist)}
    urls = [
        {
            "filename": name,
            "packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
            "url": f"https://files.pythonhosted.org/packages/test/{name}",
            "size": len(payload),
            "digests": {"sha256": hashlib.sha256(payload).hexdigest()},
            "yanked": False,
        }
        for name, payload in payloads.items()
    ]
    metadata = {
        "info": {"name": PROJECT, "version": VERSION},
        "urls": urls,
    }
    provenance = {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": pypi.TRUSTED_REPOSITORY,
                    "workflow": pypi.TRUSTED_WORKFLOW,
                    "environment": pypi.TRUSTED_ENVIRONMENT,
                },
                "attestations": [{"version": 1}],
            }
        ],
    }
    return dist, payloads, metadata, provenance


def test_public_pypi_verifies_exact_bytes_and_trusted_publisher(
    tmp_path: Path,
) -> None:
    dist, payloads, metadata, provenance = _fixture(tmp_path)
    requested: list[str] = []

    def get_json(url: str):
        requested.append(url)
        return provenance if "/integrity/" in url else metadata

    pypi.verify(
        project=PROJECT,
        version=VERSION,
        dist_dir=dist,
        get_json=get_json,
        download=lambda url: payloads[url.rsplit("/", 1)[1]],
    )

    assert requested[0] == f"https://pypi.org/pypi/{PROJECT}/{VERSION}/json"
    assert len([url for url in requested if "/integrity/" in url]) == 2


def test_public_pypi_rejects_changed_distribution_bytes(tmp_path: Path) -> None:
    dist, payloads, metadata, provenance = _fixture(tmp_path)

    with pytest.raises(pypi.PyPIVerificationError, match="bytes differ"):
        pypi.verify(
            project=PROJECT,
            version=VERSION,
            dist_dir=dist,
            get_json=lambda url: provenance if "/integrity/" in url else metadata,
            download=lambda url: (
                b"changed" if url.endswith(".whl") else payloads[url.rsplit("/", 1)[1]]
            ),
        )


def test_public_pypi_rejects_an_extra_or_missing_distribution(tmp_path: Path) -> None:
    dist, payloads, metadata, provenance = _fixture(tmp_path)
    metadata["urls"].pop()

    with pytest.raises(pypi.PyPIVerificationError, match="file set"):
        pypi.verify(
            project=PROJECT,
            version=VERSION,
            dist_dir=dist,
            get_json=lambda url: provenance if "/integrity/" in url else metadata,
            download=lambda url: payloads[url.rsplit("/", 1)[1]],
        )


@pytest.mark.parametrize("field", ["repository", "workflow", "environment"])
def test_public_pypi_rejects_wrong_trusted_publisher_identity(
    tmp_path: Path,
    field: str,
) -> None:
    dist, payloads, metadata, provenance = _fixture(tmp_path)
    provenance["attestation_bundles"][0]["publisher"][field] = "other"

    with pytest.raises(pypi.PyPIVerificationError, match="trusted workflow"):
        pypi.verify(
            project=PROJECT,
            version=VERSION,
            dist_dir=dist,
            get_json=lambda url: provenance if "/integrity/" in url else metadata,
            download=lambda url: payloads[url.rsplit("/", 1)[1]],
        )


@pytest.mark.parametrize("status", [404, 408, 429, 500, 503, 599])
def test_public_request_retries_only_transient_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    responses = iter([_http_error(status), io.BytesIO(b"published")])
    calls = []
    sleeps = []

    def open_once(request, *, timeout):
        calls.append((request.full_url, timeout))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(pypi, "urlopen", open_once)
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    assert pypi._public_bytes("https://files.pythonhosted.org/test.whl") == b"published"
    assert len(calls) == 2
    assert sleeps == [pypi.RETRY_DELAYS_SECONDS[0]]


def test_public_request_retries_network_failure_with_bounded_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def always_fails(request, *, timeout):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("temporary timeout")

    monkeypatch.setattr(pypi, "urlopen", always_fails)
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    with pytest.raises(pypi.PyPIVerificationError, match="download failed"):
        pypi._public_bytes("https://files.pythonhosted.org/test.whl")

    assert attempts == len(pypi.RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(pypi.RETRY_DELAYS_SECONDS)


def test_public_request_does_not_retry_nontransient_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def forbidden(request, *, timeout):
        nonlocal attempts
        attempts += 1
        raise _http_error(403)

    monkeypatch.setattr(pypi, "urlopen", forbidden)
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    with pytest.raises(pypi.PyPIVerificationError, match="download failed"):
        pypi._public_bytes("https://files.pythonhosted.org/test.whl")

    assert attempts == 1
    assert sleeps == []


def test_public_json_does_not_retry_semantically_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def invalid_json(request, *, timeout):
        nonlocal attempts
        attempts += 1
        return io.BytesIO(b"not json")

    monkeypatch.setattr(pypi, "urlopen", invalid_json)
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    with pytest.raises(pypi.PyPIVerificationError, match="API lookup failed"):
        pypi._public_json("https://pypi.org/pypi/veqtor-mcp/0.1.2/json")

    assert attempts == 1
    assert sleeps == []
