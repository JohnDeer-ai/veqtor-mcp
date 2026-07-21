# SPDX-License-Identifier: Apache-2.0
"""Private immutable carriers for authorized live inspection records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


_CHECKED_INSPECTION_FACTORY_TOKEN = object()
_CHECKED_INSPECTION_ERROR_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class CheckedInspectionResult:
    """Metadata-free live payload frozen after schema and bounded checks."""

    _value: Mapping[str, Any]

    def __init__(self) -> None:
        raise TypeError("CheckedInspectionResult is created by the live gate")

    @classmethod
    def _from_gate(
        cls,
        value: Mapping[str, Any],
        token: object,
    ) -> CheckedInspectionResult:
        if token is not _CHECKED_INSPECTION_FACTORY_TOKEN:
            raise TypeError("CheckedInspectionResult factory is private")
        frozen = _freeze_json(value)
        assert isinstance(frozen, Mapping)
        instance = object.__new__(cls)
        object.__setattr__(instance, "_value", frozen)
        return instance

    @property
    def view(self) -> Mapping[str, Any]:
        """Expose a recursively read-only view to the dedicated sink."""
        return self._value

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh mutable JSON copy for exactly one consumer."""
        value = _thaw_json(self._value)
        assert isinstance(value, dict)
        return value


def _checked_inspection_result_from_gate(
    value: Mapping[str, Any],
) -> CheckedInspectionResult:
    """Create the carrier at the sole post-validation call site."""
    if not isinstance(value, Mapping):
        raise TypeError("checked inspection result must be a mapping")
    return CheckedInspectionResult._from_gate(
        value,
        _CHECKED_INSPECTION_FACTORY_TOKEN,
    )


@dataclass(frozen=True, slots=True, init=False)
class CheckedInspectionError:
    """Exact-shape trusted error plus frozen provenance for the error sink."""

    _error_code: str
    _error: str
    _provenance: Mapping[str, Any]

    def __init__(self) -> None:
        raise TypeError("CheckedInspectionError is created by the error gate")

    @classmethod
    def _from_gate(
        cls,
        *,
        error_code: str,
        error: str,
        provenance: Mapping[str, Any],
        token: object,
    ) -> CheckedInspectionError:
        if token is not _CHECKED_INSPECTION_ERROR_FACTORY_TOKEN:
            raise TypeError("CheckedInspectionError factory is private")
        if not error_code or not isinstance(error_code, str):
            raise TypeError("inspection error code must be a non-empty string")
        if not isinstance(error, str):
            raise TypeError("inspection error must be a string")
        frozen_provenance = _freeze_json(provenance)
        if not isinstance(frozen_provenance, Mapping):
            raise TypeError("inspection error provenance must be a mapping")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_error_code", error_code)
        object.__setattr__(instance, "_error", error)
        object.__setattr__(instance, "_provenance", frozen_provenance)
        return instance

    def result_dict(self) -> dict[str, str]:
        """Return the only live inspection error result shape."""
        return {
            "status": "error",
            "error_code": self._error_code,
            "error": self._error,
        }

    def provenance_dict(self) -> dict[str, Any]:
        """Return one mutable provenance copy for the dedicated sink."""
        value = _thaw_json(self._provenance)
        assert isinstance(value, dict)
        return value


def _checked_inspection_error_from_gate(
    *,
    error_code: str,
    error: str,
    provenance: Mapping[str, Any],
) -> CheckedInspectionError:
    """Create exact-shape error authority without accepting a raw result."""
    return CheckedInspectionError._from_gate(
        error_code=error_code,
        error=error,
        provenance=provenance,
        token=_CHECKED_INSPECTION_ERROR_FACTORY_TOKEN,
    )
