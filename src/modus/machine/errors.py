"""Stable machine-layer failure taxonomy: every hard failure carries a code.

Machine-layer failures are split in two, and only the first kind lives here:

- **Hard failures** stop the build or the query and raise :class:`MachineError`
  with a stable ``machine.<layer>.<kebab-reason>`` code. They mean an input
  drifted, an artifact was tampered with, or a caller violated a contract.
- **Degradations** (a missing compiler, one unparseable file) never raise.
  They continue with reduced scope and must emit a coverage receipt instead;
  see :mod:`modus.machine.states`.

:class:`MachineError` deliberately subclasses :class:`ValueError` so existing
callers that catch ``ValueError`` around machine APIs keep working while new
callers can match on the structured hierarchy and code.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: Machine-layer segments allowed in error and reason codes. ``pipeline`` is
#: the orchestrator; ``store`` and ``runtime`` are the cross-cutting planes.
CODE_SEGMENTS: Final = frozenset(
    {
        "corpus",
        "facts",
        "resolution",
        "capability",
        "projection",
        "packet",
        "store",
        "runtime",
        "pipeline",
    }
)

_CODE_PATTERN: Final = re.compile(
    r"^machine\.(?P<segment>[a-z]+)\.(?P<reason>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)


def validate_code(code: str) -> str:
    """Return *code* when it is a canonical ``machine.<layer>.<kebab>`` code.

    Raises :class:`ValueError` (not :class:`MachineError`, to avoid recursion)
    when the code itself is malformed; a malformed code is a programming error
    in Modus, never a runtime condition.
    """
    match = _CODE_PATTERN.match(code)
    if match is None or match.group("segment") not in CODE_SEGMENTS:
        raise ValueError(f"malformed machine error code: {code!r}")
    return code


class MachineError(ValueError):
    """A hard machine-layer failure with a stable, machine-readable code.

    ``str(error)`` starts with the code so logs and CLI surfaces stay
    grep-able, mirroring the pre-existing ``ValueError("code: detail")``
    convention.
    """

    def __init__(self, code: str, message: str = "", *, details: dict[str, Any] | None = None):
        self.code = validate_code(code)
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {message}" if message else self.code)


class ContractError(MachineError):
    """A caller violated a machine-layer input or type contract."""


class IntegrityError(MachineError):
    """A published artifact failed digest, checksum or structure verification."""


class DriftError(MachineError):
    """A frozen input changed between capture and publication."""


class BudgetError(MachineError):
    """A request declared a budget the machine layer cannot honour."""


class ConfigurationError(MachineError):
    """Repository or machine configuration violates the analysis contract."""


class ProviderError(MachineError):
    """A required observation provider could not produce its contracted result."""


class InvariantError(MachineError):
    """An internal invariant failed after caller input had been validated."""


def safe_error_summary(error: BaseException) -> str:
    """Return a stable diagnostic summary without paths or exception text."""
    if isinstance(error, MachineError):
        return error.code
    return type(error).__name__


__all__ = [
    "BudgetError",
    "CODE_SEGMENTS",
    "ConfigurationError",
    "ContractError",
    "DriftError",
    "IntegrityError",
    "InvariantError",
    "MachineError",
    "ProviderError",
    "safe_error_summary",
    "validate_code",
]
