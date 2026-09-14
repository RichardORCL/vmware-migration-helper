"""OCI inventory lookups feeding the target form in the web UI."""

from __future__ import annotations

import logging
from typing import Optional

from helper_app.config import Settings
from helper_app.models import OciCompartment, OciOptions, OciShape, OciSubnet, OciVcn
from helper_app.oci.clients import OciClients

log = logging.getLogger(__name__)


def _all(fn, **kwargs):
    import oci

    return oci.pagination.list_call_get_all_results(fn, **kwargs).data


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
        if not s.shape.startswith("VM.") or s.shape in seen:
            continue
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


def build_options(c: OciClients, settings: Settings, compartment_id: Optional[str] = None) -> OciOptions:
    ident = c.identity_info
    comp = compartment_id or ident.compartment_id
    vcns = list_vcns(c, comp)
    return OciOptions(
        region=ident.region,
        helper_instance_id=ident.instance_id,
        helper_compartment_id=ident.compartment_id,
        helper_availability_domain=ident.availability_domain,
        default_shape=settings.default_shape,
        compartments=list_compartments(c),
        availability_domains=list_availability_domains(c),
        vcns=vcns,
        subnets=list_subnets(c, comp, vcns),
        shapes=list_flex_shapes(c, comp, ident.availability_domain),
    )
