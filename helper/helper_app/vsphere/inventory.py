"""vCenter inventory: VM list for the UI and ``VmSpec`` extraction for a selected VM."""

from __future__ import annotations

import logging

from helper_app.models import DiskSpec, Firmware, NicSpec, VmSpec, VmSummary

log = logging.getLogger(__name__)


class PreflightError(ValueError):
    pass


def _controller_type(ctrl) -> str:
    from pyVmomi import vim

    d = vim.vm.device
    if isinstance(ctrl, d.ParaVirtualSCSIController):
        return "pvscsi"
    if isinstance(ctrl, d.VirtualLsiLogicSASController):
        return "lsilogicsas"
    if isinstance(ctrl, d.VirtualLsiLogicController):
        return "lsilogic"
    if isinstance(ctrl, d.VirtualBusLogicController):
        return "buslogic"
    if isinstance(ctrl, d.VirtualIDEController):
        return "ide"
    if isinstance(ctrl, d.VirtualAHCIController) or isinstance(ctrl, d.VirtualSATAController):
        return "sata"
    if isinstance(ctrl, d.VirtualNVMEController):
        return "nvme"
    return "unknown"


def _nic_type(nic) -> str:
    from pyVmomi import vim

    d = vim.vm.device
    for cls, name in (
        (d.VirtualVmxnet3, "vmxnet3"),
        (d.VirtualVmxnet2, "vmxnet2"),
        (d.VirtualE1000e, "e1000e"),
        (d.VirtualE1000, "e1000"),
        (d.VirtualPCNet32, "pcnet32"),
        (d.VirtualSriovEthernetCard, "sriov"),
    ):
        if isinstance(nic, cls):
            return name
    return "unknown"


def _network_name(nic) -> str:
    backing = getattr(nic, "backing", None)
    if backing is None:
        return ""
    name = getattr(backing, "deviceName", None)
    if name:
        return str(name)
    port = getattr(backing, "port", None)
    if port is not None:
        return f"dvportgroup:{getattr(port, 'portgroupKey', '')}"
    return ""


_CONTROLLER_ORDER = {"ide": 0, "buslogic": 1, "lsilogic": 1, "lsilogicsas": 1, "pvscsi": 1, "sata": 2, "nvme": 3}


def esxi_host_name(vm) -> str:
    """Name of the ESXi host the VM is registered on (``vm.runtime.host.name``), '' when unknown."""
    try:
        host = vm.runtime.host
    except Exception:  # noqa: BLE001 - property fetch may fail on a stale object
        return ""
    if host is None:
        return ""
    return str(getattr(host, "name", "") or "").strip()


def vm_spec_from_vm(vm) -> VmSpec:
    from pyVmomi import vim

    d = vim.vm.device
    config = vm.config
    hardware = config.hardware
    controllers = {dev.key: dev for dev in hardware.device if isinstance(dev, d.VirtualController)}

    disks = []
    for dev in hardware.device:
        if not isinstance(dev, d.VirtualDisk):
            continue
        ctrl = controllers.get(dev.controllerKey)
        ctype = _controller_type(ctrl) if ctrl is not None else "unknown"
        backing = dev.backing
        capacity = getattr(dev, "capacityInBytes", None) or (dev.capacityInKB * 1024)
        disks.append(
            (
                (_CONTROLLER_ORDER.get(ctype, 9), getattr(ctrl, "busNumber", 0), dev.unitNumber or 0, dev.key),
                DiskSpec(
                    index=0,
                    label=dev.deviceInfo.label if dev.deviceInfo else f"disk-{dev.key}",
                    device_key=dev.key,
                    capacity_bytes=int(capacity),
                    controller_type=ctype,
                    controller_class=type(ctrl).__name__.split(".")[-1] if ctrl is not None else "",
                    controller_bus=int(getattr(ctrl, "busNumber", 0) or 0),
                    unit_number=int(dev.unitNumber or 0),
                    thin_provisioned=bool(getattr(backing, "thinProvisioned", False)),
                    backing_file=str(getattr(backing, "fileName", "") or ""),
                ),
            )
        )
    disks.sort(key=lambda t: t[0])
    disk_specs = []
    for idx, (_, spec) in enumerate(disks):
        spec.index = idx
        disk_specs.append(spec)

    nics = [
        NicSpec(label=dev.deviceInfo.label if dev.deviceInfo else "nic", adapter_type=_nic_type(dev),
                mac_address=getattr(dev, "macAddress", "") or "", network=_network_name(dev))
        for dev in hardware.device
        if isinstance(dev, d.VirtualEthernetCard)
    ]

    boot_options = getattr(config, "bootOptions", None)
    firmware = Firmware.EFI if (config.firmware or "bios").lower() == "efi" else Firmware.BIOS
    return VmSpec(
        moid=vm._moId,
        name=config.name,
        instance_uuid=config.instanceUuid or "",
        num_cpu=int(hardware.numCPU),
        memory_mb=int(hardware.memoryMB),
        guest_id=config.guestId or "otherGuest",
        guest_full_name=config.guestFullName or "",
        firmware=firmware,
        secure_boot=bool(getattr(boot_options, "efiSecureBootEnabled", False)),
        power_state=str(vm.runtime.powerState),
        has_snapshots=vm.snapshot is not None,
        host_name=esxi_host_name(vm),
        disks=disk_specs,
        nics=nics,
    )


def preflight(spec: VmSpec) -> list[str]:
    """Return blocking problems (empty list means the VM can be exported)."""
    problems = []
    if spec.power_state != "poweredOff":
        problems.append(f"VM must be powered off (current state: {spec.power_state})")
    if not spec.disks:
        problems.append("VM has no virtual disks")
    return problems


def warnings(spec: VmSpec) -> list[str]:
    notes = []
    if spec.has_snapshots:
        notes.append("VM has snapshots; the export contains the current (consolidated) disk state")
    if spec.secure_boot:
        notes.append("UEFI Secure Boot is enabled on the source; it is not re-enabled on the OCI instance")
    if spec.num_cpu % 2:
        notes.append(f"{spec.num_cpu} vCPUs round up to {(spec.num_cpu + 1) // 2} OCPUs")
    for disk in spec.disks:
        if disk.capacity_bytes < 50 * 1024**3:
            notes.append(f"{disk.label} is smaller than 50 GB; the OCI volume will be 50 GB (minimum)")
    return notes


# --------------------------------------------------------------------------- #
# VM list (PropertyCollector over a ContainerView)
# --------------------------------------------------------------------------- #
_VM_PROPS = [
    "name",
    "parent",
    "runtime.powerState",
    "config.template",
    "config.guestFullName",
    "config.guestId",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "config.hardware.device",
]


def _retrieve(si, types: list, props: list[str]) -> list[tuple[object, dict]]:
    """Fetch ``props`` for every object of ``types`` below the root folder in one PropertyCollector call."""
    from pyVmomi import vmodl

    content = si.content
    view = content.viewManager.CreateContainerView(content.rootFolder, types, True)
    try:
        traversal = vmodl.query.PropertyCollector.TraversalSpec(
            name="view", path="view", skip=False, type=type(view)
        )
        object_spec = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
        prop_specs = [vmodl.query.PropertyCollector.PropertySpec(type=t, pathSet=props, all=False) for t in types]
        filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[object_spec], propSet=prop_specs)
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        result: list[tuple[object, dict]] = []
        pc = content.propertyCollector
        page = pc.RetrievePropertiesEx([filter_spec], options)
        while page is not None:
            for oc in page.objects:
                values = {p.name: p.val for p in (oc.propSet or [])}
                result.append((oc.obj, values))
            if not page.token:
                break
            page = pc.ContinueRetrievePropertiesEx(page.token)
        return result
    finally:
        try:
            view.Destroy()
        except Exception:  # noqa: BLE001
            pass


def _folder_paths(si) -> dict[str, str]:
    """Map folder / datacenter moid -> human readable path such as 'DC1/Prod/Web'."""
    from pyVmomi import vim

    nodes: dict[str, tuple[str, str | None, str]] = {}  # moid -> (name, parent moid, kind)
    for obj, vals in _retrieve(si, [vim.Folder, vim.Datacenter], ["name", "parent"]):
        parent = vals.get("parent")
        nodes[obj._moId] = (vals.get("name", ""), parent._moId if parent is not None else None, type(obj).__name__)
    root_id = si.content.rootFolder._moId
    cache: dict[str, str] = {root_id: ""}

    def path_of(moid: str) -> str:
        if moid in cache:
            return cache[moid]
        node = nodes.get(moid)
        if node is None:
            cache[moid] = ""
            return ""
        name, parent, kind = node
        parent_path = path_of(parent) if parent else ""
        # the datacenter's implicit "vm" folder adds no information
        if kind.endswith("Folder") and name == "vm" and parent and nodes.get(parent, ("", None, ""))[2].endswith(
            "Datacenter"
        ):
            result = parent_path
        else:
            result = f"{parent_path}/{name}" if parent_path else name
        cache[moid] = result
        return result

    return {moid: path_of(moid) for moid in nodes}


def list_vm_summaries(si) -> list[VmSummary]:
    from pyVmomi import vim

    d = vim.vm.device
    folders = _folder_paths(si)
    rows: list[VmSummary] = []
    for obj, vals in _retrieve(si, [vim.VirtualMachine], _VM_PROPS):
        devices = vals.get("config.hardware.device") or []
        disks = [dev for dev in devices if isinstance(dev, d.VirtualDisk)]
        capacity = sum(int(getattr(dev, "capacityInBytes", None) or dev.capacityInKB * 1024) for dev in disks)
        parent = vals.get("parent")
        rows.append(
            VmSummary(
                moid=obj._moId,
                name=vals.get("name", obj._moId),
                folder=folders.get(parent._moId, "") if parent is not None else "",
                power_state=str(vals.get("runtime.powerState", "")),
                guest_full_name=vals.get("config.guestFullName") or "",
                guest_id=vals.get("config.guestId") or "",
                num_cpu=int(vals.get("config.hardware.numCPU") or 0),
                memory_mb=int(vals.get("config.hardware.memoryMB") or 0),
                num_disks=len(disks),
                disk_capacity_bytes=capacity,
                is_template=bool(vals.get("config.template", False)),
            )
        )
    rows.sort(key=lambda r: (r.folder.lower(), r.name.lower()))
    return rows
