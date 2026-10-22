"""NetBox objects this agent relies on: two custom fields and one tag.

Ownership model (see README "Ownership and safety"): a NetBox VM is managed by
an agent only if **all** of these hold:

* it is in the agent's configured NetBox cluster,
* it carries the ``tag`` tag (configurable via ``netbox.tag``),
* custom field ``proxmox_source`` equals the agent's ``sync.source_id``,
* custom field ``proxmox_vmid`` is set.

IP addresses created by the agent carry the same tag, which is how the
cleanup policy recognises IPs it may delete.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..errors import AuthenticationError, FatalStartupError
from ..logging_setup import get_logger, log
from .client import EP_CLUSTER_GROUPS, EP_CLUSTERS, EP_CUSTOM_FIELDS, EP_TAGS, NetBoxClient, obj_id

logger = get_logger("netbox.setup")

CF_VMID = "proxmox_vmid"
CF_SOURCE = "proxmox_source"
VM_OBJECT_TYPE = "virtualization.virtualmachine"

CUSTOM_FIELDS: dict[str, dict[str, Any]] = {
    CF_VMID: {"type": "integer", "label": "Proxmox VMID",
              "description": "Proxmox VMID (managed by netbox-pve-sync, do not edit)"},
    CF_SOURCE: {"type": "text", "label": "Proxmox source",
                "description": "Proxmox environment that owns this VM (managed by netbox-pve-sync)"},
}


@dataclass(frozen=True)
class NetBoxPrereqs:
    cluster_id: int
    cluster_name: str
    tag_id: int
    vrf_id: int | None
    site_id: int | None = None


def _cf_type(cf: dict[str, Any]) -> str:
    value = cf.get("type")
    return str(value.get("value") if isinstance(value, dict) else value)


def _cf_object_types(cf: dict[str, Any]) -> list[str]:
    return list(cf.get("object_types") or cf.get("content_types") or [])


def cluster_site_id(cluster: dict[str, Any]) -> int | None:
    """The site a cluster is assigned to: ``site`` before NetBox 4.2, ``scope`` (type dcim.site) since."""
    if cluster.get("site"):
        return obj_id(cluster["site"])
    if cluster.get("scope_type") == "dcim.site":
        return obj_id(cluster.get("scope_id") or cluster.get("scope"))
    return None


def missing_prerequisites(client: NetBoxClient, tag_slug: str) -> list[str]:
    problems = []
    for name, spec in CUSTOM_FIELDS.items():
        cf = client.custom_field(name)
        if cf is None:
            problems.append(f"custom field '{name}' does not exist")
            continue
        if _cf_type(cf) != spec["type"]:
            problems.append(f"custom field '{name}' must be of type {spec['type']} (is {_cf_type(cf)})")
        if VM_OBJECT_TYPE not in _cf_object_types(cf):
            problems.append(f"custom field '{name}' is not enabled for virtual machines")
    if client.tag(tag_slug) is None:
        problems.append(f"tag '{tag_slug}' does not exist")
    return problems


def ensure_prerequisites(client: NetBoxClient, tag_slug: str) -> list[str]:
    """Create the custom fields and tag if missing (``netbox-pve-sync init-netbox``). Returns actions taken."""
    actions = []
    types_key = client.compat.custom_field_types_key
    for name, spec in CUSTOM_FIELDS.items():
        cf = client.custom_field(name)
        if cf is None:
            client.create(EP_CUSTOM_FIELDS, {
                "name": name, "label": spec["label"], "type": spec["type"],
                types_key: [VM_OBJECT_TYPE], "description": spec["description"],
                "required": False, "filter_logic": "exact",
            }, describe=f"custom field {name}")
            actions.append(f"created custom field '{name}'")
        elif VM_OBJECT_TYPE not in _cf_object_types(cf):
            client.update(EP_CUSTOM_FIELDS, cf["id"], {types_key: [*_cf_object_types(cf), VM_OBJECT_TYPE]},
                          describe=f"custom field {name}")
            actions.append(f"enabled custom field '{name}' for virtual machines")
        elif _cf_type(cf) != spec["type"]:
            raise FatalStartupError(
                f"Custom field '{name}' exists with type {_cf_type(cf)}; it must be {spec['type']}. "
                "Rename or delete the existing field first.")
    if client.tag(tag_slug) is None:
        client.create(EP_TAGS, {"name": tag_slug, "slug": tag_slug, "color": "2196f3",
                                "description": "Object managed by netbox-pve-sync"},
                      describe=f"tag {tag_slug}")
        actions.append(f"created tag '{tag_slug}'")
    for action in actions:
        log(logger, logging.INFO, f"NetBox setup: {action}")
    return actions


def resolve_prerequisites(client: NetBoxClient, cluster_name: str, tag_slug: str,
                          vrf_name: str | None, site_name: str | None = None) -> NetBoxPrereqs:
    """Verify everything exists; fail clearly otherwise. Never creates clusters."""
    cluster = client.cluster(cluster_name)
    if cluster is None:
        raise FatalStartupError(
            f"NetBox cluster '{cluster_name}' does not exist. Create it in NetBox "
            "(Virtualization > Clusters) or fix netbox.cluster in the config. "
            "The agent never creates clusters on its own.")
    problems = missing_prerequisites(client, tag_slug)
    if problems:
        raise FatalStartupError("NetBox is not prepared: " + "; ".join(problems)
                                + ". Run 'netbox-pve-sync init-netbox' (see README).")
    tag = client.tag(tag_slug)
    vrf_id = None
    if vrf_name:
        vrf = client.vrf(vrf_name)
        if vrf is None:
            raise FatalStartupError(f"NetBox VRF '{vrf_name}' (netbox.vrf) does not exist")
        vrf_id = int(vrf["id"])
    site_id = None
    if site_name:
        site = client.site(site_name)
        if site is None:
            raise FatalStartupError(f"NetBox site '{site_name}' (netbox.site) does not exist")
        site_id = int(site["id"])
        # NetBox rejects a VM whose site differs from the site its cluster is assigned to.
        pinned = cluster_site_id(cluster)
        if pinned is not None and pinned != site_id:
            raise FatalStartupError(
                f"NetBox cluster '{cluster_name}' is assigned to another site (id {pinned}) than "
                f"netbox.site '{site_name}' (id {site_id}); NetBox would reject every VM. "
                "Change the cluster's site in NetBox or fix netbox.site.")
    assert tag is not None
    return NetBoxPrereqs(cluster_id=int(cluster["id"]), cluster_name=cluster_name, tag_id=int(tag["id"]),
                         vrf_id=vrf_id, site_id=site_id)


def ensure_cluster_group(client: NetBoxClient, cluster_name: str, group_name: str) -> list[str]:
    """Put the configured cluster in cluster group ``group_name``, creating the group if needed.

    Best effort: without permission to add cluster groups or change clusters it only warns.
    """
    actions: list[str] = []
    cluster = client.cluster(cluster_name)
    if cluster is None:
        return actions
    try:
        group = client.cluster_group(group_name)
        if group is None:
            slug = re.sub(r"[^a-z0-9_]+", "-", group_name.lower()).strip("-") or "proxmox"
            group = client.create(EP_CLUSTER_GROUPS, {"name": group_name, "slug": slug},
                                  describe=f"cluster group {group_name}")
            actions.append(f"created cluster group '{group_name}'")
        if obj_id(cluster.get("group")) != int(group["id"]):
            client.update(EP_CLUSTERS, int(cluster["id"]), {"group": int(group["id"])},
                          describe=f"cluster {cluster_name}: group {group_name}")
            actions.append(f"set group of cluster '{cluster_name}' to '{group_name}'")
    except AuthenticationError as exc:
        log(logger, logging.WARNING, f"Cannot set cluster group '{group_name}' on cluster '{cluster_name}': "
            "the API token lacks permission (add cluster group / change cluster)", error=str(exc))
        return actions
    for action in actions:
        log(logger, logging.INFO, f"NetBox setup: {action}")
    return actions
