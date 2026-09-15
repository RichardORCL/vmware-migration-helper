# ----------------------------------------------------------------------------- injected by Resource Manager
variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type = string
}

variable "compartment_ocid" {
  description = "Compartment for the helper instance, the seed image bucket and (by default) the seed images"
  type        = string
}

# ----------------------------------------------------------------------------- placement
variable "availability_domain" {
  description = "AD of the helper VM. Target instances can only be created in this AD (boot volumes are AD-local)."
  type        = string
}

variable "vcn_ocid" {
  description = "VCN containing the helper subnet"
  type        = string
}

variable "subnet_ocid" {
  description = "Subnet for the helper VM; must route to vCenter (VPN/FastConnect) and be reachable from the administrators' browsers"
  type        = string
}

variable "assign_public_ip" {
  description = "Give the helper a public IP (only when administrators reach the web UI over the internet)"
  type        = bool
  default     = false
}

variable "allowed_source_cidrs" {
  description = "CIDRs allowed to reach the web UI (TCP 8443) and SSH (TCP 22): the administrators' networks"
  type        = list(string)
}

# ----------------------------------------------------------------------------- vCenter
variable "vcenter_host" {
  description = "Default vCenter Server host name or IP as reachable from the helper subnet (SOAP API and NFC disk download on 443); users may enter another vCenter on the login page"
  type        = string
}

variable "vcenter_port" {
  description = "vCenter HTTPS port"
  type        = number
  default     = 443
}

variable "vcenter_verify_ssl" {
  description = "Verify the vCenter TLS certificate (disable for self-signed certificates)"
  type        = bool
  default     = false
}

# ----------------------------------------------------------------------------- instance
variable "helper_display_name" {
  type    = string
  default = "vc-oci-helper"
}

variable "helper_shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
}

variable "helper_ocpus" {
  type    = number
  default = 2
}

variable "helper_memory_gb" {
  type    = number
  default = 16
}

variable "ssh_public_key" {
  description = "SSH public key for the opc user"
  type        = string
}

# ----------------------------------------------------------------------------- helper service
variable "seed_bucket_name" {
  description = "Object Storage bucket used while importing seed custom images"
  type        = string
  default     = "vc-oci-seed-images"
}

variable "default_target_shape" {
  description = "Flex shape used for migrated instances"
  type        = string
  default     = "VM.Standard.E5.Flex"
}

variable "max_concurrent_jobs" {
  description = "Number of migrations the helper runs in parallel (each needs its volumes attached; 32 attachment slots in total)"
  type        = number
  default     = 2
}

variable "source_git_url" {
  description = "Git repository containing the helper; cloned on the VM and installed with pip"
  type        = string
  default     = "https://github.com/RichardORCL/vmware-migration-helper.git"
}

variable "source_git_ref" {
  description = "Branch, tag or commit to install"
  type        = string
  default     = "main"
}

# ----------------------------------------------------------------------------- IAM
variable "create_iam" {
  description = "Create the dynamic group, policy and tag namespace (requires tenancy administrator rights). Disable if an administrator created them separately."
  type        = bool
  default     = true
}

variable "policy_scope_compartment_ocid" {
  description = "Compartment in which the helper may create target instances and volumes (empty = whole tenancy)"
  type        = string
  default     = ""
}
