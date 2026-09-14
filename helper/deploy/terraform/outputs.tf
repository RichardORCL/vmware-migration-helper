locals {
  helper_ip = var.assign_public_ip ? oci_core_instance.helper.public_ip : oci_core_instance.helper.private_ip
}

output "helper_instance_id" {
  value = oci_core_instance.helper.id
}

output "helper_private_ip" {
  value = oci_core_instance.helper.private_ip
}

output "helper_public_ip" {
  value = oci_core_instance.helper.public_ip
}

output "helper_ui_url" {
  description = "Web UI of the migration helper"
  value       = "https://${local.helper_ip}:8443/"
}

output "availability_domain" {
  description = "Target instances must be created in this AD"
  value       = var.availability_domain
}

output "vcenter_host" {
  value = var.vcenter_host
}

output "seed_bucket" {
  value = oci_objectstorage_bucket.seed.name
}

output "next_steps" {
  value = <<-EOT
    1. Wait ~3 minutes for cloud-init, then: curl -k https://${local.helper_ip}:8443/api/health
    2. Make sure the helper can reach vCenter: from the VM, curl -k https://${var.vcenter_host}:${var.vcenter_port}/sdk
    3. Open https://${local.helper_ip}:8443/ in a browser, accept the self-signed certificate and log in with a
       vCenter account that has read access to the inventory and "Allow disk access" / "Export" on the VMs to migrate.
    4. Pick a powered-off VM under "Virtual machines" and start the migration.
  EOT
}
