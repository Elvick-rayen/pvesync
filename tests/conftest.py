from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from netbox_pve_sync.config import Config, parse_config
from netbox_pve_sync.netbox.client import NetBoxClient
from netbox_pve_sync.proxmox.client import ProxmoxClient
from netbox_pve_sync.service import SyncService
from netbox_pve_sync.state import StateStore

from .fakes import FakeNetBox, FakeProxmox

NB_TOKEN = "nb-test-token-1234567890"
PVE_SECRET = "11111111-2222-3333-4444-555555555555"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def raw_config(**sections: dict[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "netbox": {"url": "http://netbox.test:8000", "token": "${NETBOX_API_TOKEN}", "cluster": "PVE-Test"},
        "proxmox": {"api_url": "https://127.0.0.1:8006", "token_id": "netbox-sync@pve!agent",
                    "token_secret": "${PVE_TOKEN_SECRET}"},
        "retry": {"jitter": 0},
    }
    for name, values in sections.items():
        raw.setdefault(name, {}).update(values)
    return raw


def make_config(tmp_path: Path, **sections: dict[str, Any]) -> Config:
    raw = raw_config(**sections)
    raw.setdefault("state", {})["path"] = str(tmp_path / "state.db")
    return parse_config(raw, env={"NETBOX_API_TOKEN": NB_TOKEN, "PVE_TOKEN_SECRET": PVE_SECRET})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def netbox() -> FakeNetBox:
    nb = FakeNetBox(token=NB_TOKEN)
    nb.prepare()
    return nb


@pytest.fixture
def proxmox() -> FakeProxmox:
    return FakeProxmox()


ServiceFactory = Callable[..., SyncService]


@pytest.fixture
def make_service(tmp_path: Path, netbox: FakeNetBox, proxmox: FakeProxmox, clock: FakeClock) -> ServiceFactory:
    def factory(*, dry_run: bool = False, nb: FakeNetBox | None = None, px: FakeProxmox | None = None,
                **sections: dict[str, Any]) -> SyncService:
        config = make_config(tmp_path, **sections)
        service = SyncService(
            config,
            proxmox=ProxmoxClient(config.proxmox, session=px or proxmox),
            netbox=NetBoxClient(config.netbox, session=nb or netbox, dry_run=dry_run),
            state=StateStore(config.state_path, clock=clock),
            dry_run=dry_run,
            clock=clock,
        )
        return service
    return factory


@pytest.fixture
def service(make_service: ServiceFactory) -> SyncService:
    svc = make_service()
    svc.check_proxmox()
    svc.check_netbox()
    return svc


@pytest.fixture(autouse=True)
def _quiet_logs(caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger("netbox_pve_sync").propagate = True
    caplog.set_level(logging.DEBUG, logger="netbox_pve_sync")
