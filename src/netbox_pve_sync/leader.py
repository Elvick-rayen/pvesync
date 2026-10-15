"""Active/standby selection for multi-node Proxmox clusters.

All nodes of a PVE cluster see the same VMs, so if the agent is installed on
every node exactly one of them must act. The rule needs no lock and no extra
service: the **first online node is active** (``GET /cluster/status``),
where "first" means the lowest corosync node ID - or, if
``proxmox.agent_nodes`` is set, the first online node of that list. Set
``agent_nodes`` when the agent is *not* installed on every node, otherwise
an online node without the agent would be chosen and nothing would sync.
If the active node goes down, the next one takes over within about 30
seconds and immediately reconciles. A standalone node is always active.

During a network partition two nodes could briefly both be active; that is
safe because every write is idempotent and ownership-checked.
"""

from __future__ import annotations

import logging

from .errors import AuthenticationError, TransientApiError
from .logging_setup import get_logger, log
from .proxmox.client import ProxmoxClient

logger = get_logger("leader")


class LeaderElector:
    def __init__(self, client: ProxmoxClient, *, enabled: bool = True, agent_nodes: tuple[str, ...] = ()) -> None:
        self.client = client
        self.enabled = enabled
        self.agent_nodes = agent_nodes
        self.active = not enabled
        self._known = not enabled

    def check(self) -> bool:
        """Update ``active``. Returns True when this node just became active."""
        if not self.enabled:
            return False
        try:
            info = self.client.cluster_info()
        except (TransientApiError, AuthenticationError) as exc:
            log(logger, logging.WARNING, "Cannot read cluster status; keeping current role",
                active=self.active, error=str(exc))
            return False
        online = [name for _, name in info.online_nodes]
        if self.agent_nodes:
            online = [n for n in self.agent_nodes if n in online]
        if info.local_node is None or (not online and not self.agent_nodes):
            now_active = True  # standalone node, or no cluster information
            active_node = info.local_node
        else:
            active_node = online[0] if online else None
            now_active = active_node == info.local_node
        became = now_active and not self.active
        if now_active != self.active or not self._known:
            if now_active:
                log(logger, logging.INFO, "This node is the active synchronization agent", node=info.local_node,
                    cluster=info.cluster_name or "(standalone)")
            else:
                log(logger, logging.INFO, "Standing by: another node is the active agent", node=info.local_node,
                    active_node=active_node or "(none online)")
        self.active = now_active
        self._known = True
        return became
