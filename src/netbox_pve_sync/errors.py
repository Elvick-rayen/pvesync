"""Exception hierarchy.

The split between *transient* and *permanent* errors drives the retry queue:
transient errors (network down, HTTP 5xx, timeouts) are retried with backoff
and never lose the event; permanent errors (validation, conflicts) are logged
clearly and retried slowly, because a human usually has to fix something.
"""

from __future__ import annotations


class SyncError(Exception):
    """Base class for all errors raised by netbox-pve-sync."""


class ConfigError(SyncError):
    """The configuration file is missing, malformed or inconsistent."""


class FatalStartupError(SyncError):
    """A startup check failed in a way that retrying cannot fix (wrong token, missing cluster)."""


class ApiError(SyncError):
    """An API call failed."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class TransientApiError(ApiError):
    """Network failure, timeout or HTTP 5xx/429: safe to retry later."""


class AuthenticationError(ApiError):
    """HTTP 401/403: the token is invalid, expired or lacks permissions."""


class NotFoundError(ApiError):
    """HTTP 404 or an equivalent 'does not exist' answer."""


class ValidationError(ApiError):
    """HTTP 400: the server rejected the data (NetBox validation error)."""


class NetBoxUnavailable(TransientApiError):
    """NetBox cannot be reached. The sync is queued and retried."""


class ProxmoxUnavailable(TransientApiError):
    """The Proxmox API cannot be reached."""


class ConflictError(SyncError):
    """NetBox contains data this agent must not overwrite (e.g. an IP owned by another object)."""
