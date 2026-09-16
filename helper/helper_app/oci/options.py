"""OCI inventory lookups feeding the target form in the web UI."""

from __future__ import annotations

import logging
from typing import Optional

from helper_app.config import Settings
from helper_app.models import OciCompartment, OciOptions, OciShape, OciSubnet, OciVcn
from helper_app.oci.clients import OciClients
from helper_app.oci.mapping import is_arm_shape

log = logging.getLogger(__name__)


def _all(fn, **kwargs):
    import oci

    return oci.pagination.list_call_get_all_results(fn, **kwargs).data


class PrivateIpError(ValueError):
    """The requested fixed private IP cannot be used in the subnet (message is user facing)."""


def check_private_ip(c: OciClients, subnet_id: str, ip: str) -> None:
    """Verify a fixed private IP against OCI: inside the subnet's CIDR, not one of the addresses the
    Networking service reserves (the first two and the last of the CIDR) and not already allocated to
    a VNIC in that subnet.  Raises PrivateIpError; other exceptions mean the check itself failed."""
    from ipaddress import IPv4Address, IPv4Network

    subnet = c.network.get_subnet(subnet_id).data
    cidr = getattr(subnet, "cidr_block", "") or ""
    net = IPv4Network(cidr, strict=False)
    addr = IPv4Address(ip)
    if addr not in net:
        raise PrivateIpError(f"{ip} is not inside the CIDR {cidr} of subnet {subnet.display_name or subnet_id}")
    reserved = {net.network_address, net.network_address + 1, net.broadcast_address}
    if addr in reserved:
        raise PrivateIpError(f"{ip} is reserved by OCI in {cidr} (network address, default gateway and broadcast "
                             "cannot be assigned)")
    used = _all(c.network.list_private_ips, subnet_id=subnet_id, ip_address=ip)
    if used:
        owner = getattr(used[0], "hostname_label", None) or getattr(used[0], "display_name", None)
        raise PrivateIpError(f"{ip} is already in use in subnet {subnet.display_name or subnet_id}"
                             + (f" (by {owner})" if owner else ""))


def primary_vnic_ips(c: OciClients, instance) -> tuple[Optional[str], Optional[str]]:
    """(private IP, public IP) of the instance's primary VNIC as OCI assigned them, (None, None) while the
    VNIC is not attached yet.  ListVnicAttachments + GetVnic; the OCI Python SDK has no shortcut for this."""
    atts = _all(c.compute.list_vnic_attachments, compartment_id=instance.compartment_id, instance_id=instance.id)
    attached = [a for a in atts if a.lifecycle_state == "ATTACHED" and getattr(a, "vnic_id", None)]
    if not attached:
        return None, None
    vnics = [c.network.get_vnic(a.vnic_id).data for a in attached]
    primary = next((v for v in vnics if getattr(v, "is_primary", False)), vnics[0])
    return getattr(primary, "private_ip", None) or None, getattr(primary, "public_ip", None) or None


def list_compartments(c: OciClients) -> list[OciCompartment]:
    tenancy = c.identity_info.tenancy_id
    result: list[OciCompartment] = []
    if tenancy:
        try:
            root = c.identity.get_compartment(tenancy).data
            result.append(OciCompartment(id=root.id, name=root.name, path=root.name))
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot read root compartment: %s", exc)
            result.append(OciCompartment(id=tenancy, name="(tenancy root)", path="/"))
        comps = _all(
            c.identity.list_compartments,
            compartment_id=tenancy,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        )
        by_id = {x.id: x for x in comps}

        def path_of(comp) -> str:
            parts = [comp.name]
            parent = by_id.get(comp.compartment_id)
            while parent is not None:
                parts.append(parent.name)
                parent = by_id.get(parent.compartment_id)
            return "/".join(reversed(parts))

        result.extend(sorted((OciCompartment(id=x.id, name=x.name, path=path_of(x)) for x in comps),
                             key=lambda x: x.path.lower()))
    else:
        result.append(OciCompartment(id=c.identity_info.compartment_id, name="(helper compartment)"))
    return result


def list_availability_domains(c: OciClients) -> list[str]:
    ads = _all(c.identity.list_availability_domains, compartment_id=c.identity_info.tenancy_id or
               c.identity_info.compartment_id)
    return [a.name for a in ads]


def list_vcns(c: OciClients, compartment_id: str) -> list[OciVcn]:
    vcns = _all(c.network.list_vcns, compartment_id=compartment_id, lifecycle_state="AVAILABLE")
    result = [
        OciVcn(id=v.id, name=v.display_name,
               cidr_blocks=list(getattr(v, "cidr_blocks", None) or ([v.cidr_block] if getattr(v, "cidr_block", None)
                                                                     else [])))
        for v in vcns
    ]
    return sorted(result, key=lambda v: v.name.lower())


def list_subnets(c: OciClients, compartment_id: str, vcns: list[OciVcn]) -> list[OciSubnet]:
    """Subnets in ``compartment_id``; VCN names resolved from ``vcns`` (a subnet may live in another
    compartment than its VCN, so unknown VCNs are looked up individually)."""
    names = {v.id: v.name for v in vcns}
    subnets = _all(c.network.list_subnets, compartment_id=compartment_id, lifecycle_state="AVAILABLE")
    for s in subnets:
        if s.vcn_id not in names:
            try:
                names[s.vcn_id] = c.network.get_vcn(s.vcn_id).data.display_name
            except Exception as exc:  # noqa: BLE001
                log.debug("cannot resolve VCN %s: %s", s.vcn_id, exc)
                names[s.vcn_id] = ""
    return sorted(
        (
            OciSubnet(
                id=s.id,
                name=s.display_name,
                vcn_id=s.vcn_id,
                vcn_name=names.get(s.vcn_id, ""),
                cidr_block=s.cidr_block or "",
                availability_domain=s.availability_domain,
                prohibit_public_ip=bool(s.prohibit_public_ip_on_vnic),
            )
            for s in subnets
        ),
        key=lambda s: (s.vcn_name.lower(), s.name.lower()),
    )


def list_flex_shapes(c: OciClients, compartment_id: str, availability_domain: str) -> list[OciShape]:
    shapes = _all(c.compute.list_shapes, compartment_id=compartment_id, availability_domain=availability_domain)
    seen: dict[str, OciShape] = {}
    for s in shapes:
        if not s.shape.startswith("VM.") or s.shape in seen or is_arm_shape(s.shape):
            continue  # x86 VM shapes only: an Ampere (aarch64) instance cannot boot a vSphere guest
        is_flex = bool(getattr(s, "is_flexible", False)) or s.shape.endswith(".Flex")
        oc = getattr(s, "ocpu_options", None)
        mem = getattr(s, "memory_options", None)
        seen[s.shape] = OciShape(
            name=s.shape,
            is_flex=is_flex,
            min_ocpus=getattr(oc, "min", None) if oc else s.ocpus,
            max_ocpus=getattr(oc, "max", None) if oc else s.ocpus,
            min_memory_gb=getattr(mem, "min_in_g_bs", None) if mem else s.memory_in_gbs,
            max_memory_gb=getattr(mem, "max_in_g_bs", None) if mem else s.memory_in_gbs,
        )
    flex = [x for x in seen.values() if x.is_flex]
    return sorted(flex, key=lambda x: x.name)


def build_options(
    c: OciClients,
    settings: Settings,
    compartment_id: Optional[str] = None,
    network_compartment_id: Optional[str] = None,
) -> OciOptions:
    """``compartment_id`` is where the instance goes (shapes are listed there); the VCNs and subnets come
    from ``network_compartment_id`` (defaults to the instance compartment) - networks commonly live in a
    shared compartment."""
    ident = c.identity_info
    comp = compartment_id or ident.compartment_id
    net_comp = network_compartment_id or comp
    vcns = list_vcns(c, net_comp)
    return OciOptions(
        region=ident.region,
        helper_instance_id=ident.instance_id,
        helper_compartment_id=ident.compartment_id,
        helper_availability_domain=ident.availability_domain,
        default_shape=settings.default_shape,
        compartments=list_compartments(c),
        availability_domains=list_availability_domains(c),
        vcns=vcns,
        subnets=list_subnets(c, net_comp, vcns),
        shapes=list_flex_shapes(c, comp, ident.availability_domain),
    )
