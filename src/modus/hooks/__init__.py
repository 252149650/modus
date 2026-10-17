"""Secure, fail-open startup guidance for supported coding hosts.

The package separates host contracts, untrusted input parsing, trusted context
composition, environment persistence, installation assets, and runtime
orchestration.  The small surface below is safe for integrations; platform
adapters use :mod:`modus.hooks.bootstrap` for generated configuration.
"""

from .contracts import HOST_CONTRACTS, SUPPORTED_HOSTS, HostContract, HostName
from .protocol import HookEvent, make_session_key, parse_hook_event, read_hook_event

__all__ = [
    "HOST_CONTRACTS",
    "SUPPORTED_HOSTS",
    "HookEvent",
    "HostContract",
    "HostName",
    "make_session_key",
    "parse_hook_event",
    "read_hook_event",
]
