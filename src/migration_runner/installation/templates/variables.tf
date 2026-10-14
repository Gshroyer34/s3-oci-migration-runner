variable "tenancy_ocid" { type = string }
variable "compartment_ocid" { type = string }
variable "region" { type = string }
variable "vcn_ocid" { type = string }
variable "subnet_ocid" { type = string }
variable "availability_domain" { type = string }
variable "image_ocid" { type = string }
variable "profile_revision" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.profile_revision))
    error_message = "Use the revision from a generated installation profile."
  }
}
variable "deployment_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.deployment_name))
    error_message = "Use 3 to 24 lowercase letters, digits and hyphens, starting with a letter."
  }
}
variable "ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^(ssh-rsa|ssh-ed25519) [A-Za-z0-9+/=]+( [^\r\n]*)?$", var.ssh_public_key))
    error_message = "Enter one OpenSSH public key. Private keys are never accepted."
  }
}
variable "admin_cidr" {
  type = string
  validation {
    condition     = can(cidrnetmask(var.admin_cidr)) && try(tonumber(split("/", var.admin_cidr)[1]) >= 24, false)
    error_message = "Use a valid IPv4 administrator CIDR of /24 or narrower."
  }
}
variable "worker_shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
  validation {
    condition     = contains(["VM.Standard.E4.Flex", "VM.Standard.E5.Flex", "VM.Optimized3.Flex"], var.worker_shape)
    error_message = "Select one of the supported x86 shapes. ARM runtime packaging is not yet supported."
  }
}
variable "worker_ocpus" {
  type    = number
  default = 2
  validation {
    condition     = var.worker_ocpus >= 1 && var.worker_ocpus <= 16 && floor(var.worker_ocpus) == var.worker_ocpus
    error_message = "Choose 1 to 16 whole OCPUs."
  }
}
variable "worker_memory_gib" {
  type    = number
  default = 16
  validation {
    condition     = var.worker_memory_gib >= 8 && var.worker_memory_gib <= 256 && floor(var.worker_memory_gib) == var.worker_memory_gib
    error_message = "Choose 8 to 256 whole GiB of memory."
  }
}
variable "initial_workers" {
  type    = number
  default = 2
  validation {
    condition     = var.initial_workers >= 1 && var.initial_workers <= 4 && floor(var.initial_workers) == var.initial_workers
    error_message = "Choose 1 to 4 workers for this foundation release."
  }
}
variable "maximum_workers" {
  type    = number
  default = 4
  validation {
    condition     = var.maximum_workers >= 1 && var.maximum_workers <= 4 && floor(var.maximum_workers) == var.maximum_workers
    error_message = "Choose a maximum of 1 to 4 workers; this does not enable automatic scaling."
  }
}
variable "acknowledge_foundation_only" {
  type        = bool
  default     = false
  description = "This creates billable private hosts and a Vault. Application activation and migrations are not included. Review the plan and costs first."
}
