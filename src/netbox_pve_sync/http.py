"""Thin JSON-over-HTTP helper shared by the Proxmox and NetBox clients.

It only does one thing: send a request with a timeout and turn every failure
into one of the exception classes in :mod:`errors`, so callers can decide
"retry later" versus "this needs a human" without inspecting HTTP details.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import requests

from .errors import (
    ApiError,
    AuthenticationError,
    NotFoundError,
    TransientApiError,
    ValidationError,
)


class SessionLike(Protocol):
    headers: Any

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def make_session(verify_ssl: bool, ca_bundle: str | None) -> SessionLike:
    session = requests.Session()
    session.verify = ca_bundle if (verify_ssl and ca_bundle) else verify_ssl
    session.trust_env = False  # never route local API calls through a proxy from the environment
    return cast(SessionLike, session)


def _short_body(response: Any) -> str:
    """Reason phrase plus the start of the body. Proxmox puts its error text in the reason phrase."""
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - defensive
        text = ""
    reason = getattr(response, "reason", "") or ""
    return f"{reason} {text}".strip()[:500]


class JsonHttp:
    def __init__(self, session: SessionLike, base_url: str, timeout: float, *,
                 transient_cls: type[TransientApiError] = TransientApiError, service: str = "API") -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transient_cls = transient_cls
        self.service = service

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                json: Any = None, data: Any = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            response = self.session.request(method, url, params=params, json=json, data=data,
                                            timeout=self.timeout)
        except requests.exceptions.SSLError as exc:
            # Certificate problems do not fix themselves, but they are also not data errors:
            # treat them as "unavailable" so nothing is lost, and say clearly what is wrong.
            raise self.transient_cls(f"{self.service} TLS verification failed for {self.base_url}: {exc}") from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise self.transient_cls(
                f"{self.service} unreachable at {self.base_url}: {exc.__class__.__name__}") from exc
        except requests.exceptions.RequestException as exc:
            raise self.transient_cls(f"{self.service} request failed: {exc.__class__.__name__}") from exc

        status = response.status_code
        if status in (401, 403):
            raise AuthenticationError(
                f"{self.service} rejected the credentials for {method} {path} (HTTP {status})",
                status=status, body=_short_body(response))
        if status == 404:
            raise NotFoundError(f"{self.service} {method} {path}: not found", status=status,
                                body=_short_body(response))
        if status == 429 or status >= 500:
            raise self.transient_cls(f"{self.service} {method} {path} failed with HTTP {status}",
                                     status=status, body=_short_body(response))
        if status == 400:
            raise ValidationError(f"{self.service} rejected {method} {path}: {_short_body(response)}",
                                  status=status, body=_short_body(response))
        if status >= 300:
            raise ApiError(f"{self.service} {method} {path} failed with HTTP {status}", status=status,
                           body=_short_body(response))
        if status == 204 or not getattr(response, "content", b""):
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"{self.service} returned invalid JSON for {method} {path}", status=status) from exc
