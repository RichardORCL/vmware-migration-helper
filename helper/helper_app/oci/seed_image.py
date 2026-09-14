"""Custom *seed* images.

OCI derives an instance's firmware (BIOS / UEFI_64) and device model from the image
it is launched from.  Platform images do not let us pick those freely, so for each
(firmware, OS) combination we import a tiny placeholder VMDK as a custom image with
``launchMode=CUSTOM`` and pin its capability schema.  Instances launched from the
seed image accept explicit ``LaunchOptions`` and, for Windows, ``licensingConfigs``.
The seed's boot volume content is irrelevant: it is overwritten by the block copy.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from helper_app.config import Settings
from helper_app.disk.vmdk_stream import encode_empty_disk
from helper_app.models import LaunchOptionsSpec
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.mapping import OsMetadata, seed_image_tags

log = logging.getLogger(__name__)

SEED_TAG = "vc-oci-seed"


class SeedImageService:
    def __init__(self, clients: OciClients, settings: Settings):
        self.c = clients
        self.s = settings

    @property
    def seed_compartment(self) -> str:
        """Configured seed compartment, else the helper's own (discovered from the instance metadata)."""
        return self.s.seed_compartment_id or self.c.identity_info.compartment_id

    # ------------------------------------------------------------------ public
    def get_or_create(self, os_meta: OsMetadata, firmware: str, launch_options: LaunchOptionsSpec) -> str:
        tags = seed_image_tags(os_meta, firmware)
        existing = self.find(tags)
        if existing is not None:
            log.info("reusing seed image %s (%s)", existing.id, existing.display_name)
            return existing.id
        return self.create(os_meta, firmware, launch_options, tags)

    def find(self, tags: dict[str, str]) -> Optional[Any]:
        import oci

        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images,
            compartment_id=self.seed_compartment,
            lifecycle_state="AVAILABLE",
        ).data
        for img in images:
            ft = img.freeform_tags or {}
            if all(ft.get(k) == v for k, v in tags.items()):
                return img
        return None

    def create(
        self, os_meta: OsMetadata, firmware: str, launch_options: LaunchOptionsSpec, tags: dict[str, str]
    ) -> str:
        import oci.core.models as M
        from oci.object_storage.models import CreateBucketDetails

        namespace = self.c.object_storage.get_namespace().data
        self._ensure_bucket(namespace, CreateBucketDetails)

        display = f"vc-oci-seed-{firmware.lower()}-{os_meta.slug}"
        object_name = f"{display}-{uuid.uuid4().hex[:8]}.vmdk"
        payload = encode_empty_disk(self.s.seed_disk_size_gb * 1024**3)
        log.info("uploading %d byte placeholder VMDK to %s/%s", len(payload), self.s.seed_bucket, object_name)
        self.c.object_storage.put_object(namespace, self.s.seed_bucket, object_name, payload)

        try:
            details = M.CreateImageDetails(
                compartment_id=self.seed_compartment,
                display_name=display,
                launch_mode="CUSTOM",
                freeform_tags=tags,
                image_source_details=M.ImageSourceViaObjectStorageTupleDetails(
                    source_type="objectStorageTuple",
                    namespace_name=namespace,
                    bucket_name=self.s.seed_bucket,
                    object_name=object_name,
                    source_image_type="VMDK",
                    operating_system=os_meta.operating_system,
                    operating_system_version=os_meta.operating_system_version,
                ),
            )
            image = self.c.compute.create_image(details).data
            log.info("importing seed image %s (%s)", image.id, display)
            self.c.wait_for(
                lambda: self.c.compute.get_image(image.id),
                "lifecycle_state",
                ["AVAILABLE"],
                self.s.image_import_timeout_s,
                failure_states=("DELETED", "DISABLED"),
                what=f"seed image {display}",
            )
            self._apply_capability_schema(image.id, firmware, launch_options, display, tags)
            return image.id
        finally:
            try:
                self.c.object_storage.delete_object(namespace, self.s.seed_bucket, object_name)
            except Exception as exc:  # pragma: no cover
                log.warning("could not delete seed object %s: %s", object_name, exc)

    def cleanup(self) -> list[str]:
        """Delete every seed image in the seed compartment.  Returns deleted image ids."""
        import oci

        deleted: list[str] = []
        images = oci.pagination.list_call_get_all_results(
            self.c.compute.list_images, compartment_id=self.seed_compartment
        ).data
        for img in images:
            if (img.freeform_tags or {}).get(SEED_TAG) == "true" and img.lifecycle_state != "DELETED":
                self.c.compute.delete_image(img.id)
                deleted.append(img.id)
        return deleted

    # ----------------------------------------------------------------- private
    def _ensure_bucket(self, namespace: str, create_bucket_details_cls) -> None:
        import oci

        try:
            self.c.object_storage.get_bucket(namespace, self.s.seed_bucket)
            return
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
        self.c.object_storage.create_bucket(
            namespace,
            create_bucket_details_cls(name=self.s.seed_bucket, compartment_id=self.seed_compartment,
                                      public_access_type="NoPublicAccess"),
        )

    def _global_schema_version_name(self) -> str:
        schemas = self.c.compute.list_compute_global_image_capability_schemas().data
        if not schemas:
            raise OciError("no global image capability schema available in this region")
        schema = schemas[0]
        name = getattr(schema, "current_version_name", None)
        if name:
            return name
        versions = self.c.compute.list_compute_global_image_capability_schema_versions(schema.id).data
        if not versions:
            raise OciError("global image capability schema has no versions")
        return versions[0].name

    def _apply_capability_schema(
        self, image_id: str, firmware: str, lo: LaunchOptionsSpec, display: str, tags: dict[str, str]
    ) -> None:
        import oci.core.models as M

        def enum(values: list[str], default: str):
            return M.EnumStringImageCapabilitySchemaDescriptor(source="IMAGE", values=values, default_value=default)

        def boolean(default: bool):
            return M.BooleanImageCapabilitySchemaDescriptor(source="IMAGE", default_value=default)

        schema_data = {
            "Compute.Firmware": enum([firmware], firmware),
            "Compute.LaunchMode": enum(["CUSTOM", "PARAVIRTUALIZED", "EMULATED", "NATIVE"], "CUSTOM"),
            "Storage.BootVolumeType": enum(["PARAVIRTUALIZED", "ISCSI", "SCSI", "IDE"], lo.boot_volume_type.value),
            "Storage.RemoteDataVolumeType": enum(["PARAVIRTUALIZED", "ISCSI"], "PARAVIRTUALIZED"),
            "Network.AttachmentType": enum(["PARAVIRTUALIZED", "E1000", "VFIO"], lo.network_type.value),
            "Storage.ConsistentVolumeNaming": boolean(True),
            "Compute.SecureBoot": boolean(False),
        }
        details = M.CreateComputeImageCapabilitySchemaDetails(
            compartment_id=self.seed_compartment,
            compute_global_image_capability_schema_version_name=self._global_schema_version_name(),
            image_id=image_id,
            display_name=f"{display}-capabilities",
            freeform_tags=tags,
            schema_data=schema_data,
        )
        self.c.compute.create_compute_image_capability_schema(details)
        log.info("capability schema applied to %s: firmware=%s", image_id, firmware)
