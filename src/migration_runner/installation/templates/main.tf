data "oci_core_subnet" "selected" { subnet_id = var.subnet_ocid }
data "oci_core_vcn" "selected" { vcn_id = var.vcn_ocid }
data "oci_core_image" "selected" { image_id = var.image_ocid }
data "oci_identity_compartment" "selected" { id = var.compartment_ocid }
data "oci_identity_availability_domains" "selected" { compartment_id = var.tenancy_ocid }

locals {
  tags = { Application = "migration-runner", Deployment = var.deployment_name, ConfigurationRevision = var.profile_revision }
  nodes = merge(
    { coordinator = { ocpus = 2, memory = 16 } },
    { for i in range(var.initial_workers) : "worker-${i + 1}" => { ocpus = var.worker_ocpus, memory = var.worker_memory_gib } }
  )
}

resource "terraform_data" "preflight" {
  lifecycle {
    precondition {
      condition     = var.acknowledge_foundation_only
      error_message = "This release provisions infrastructure only. Review START_HERE.md and explicitly acknowledge the foundation scope."
    }
    precondition {
      condition     = data.oci_core_subnet.selected.prohibit_public_ip_on_vnic && data.oci_core_subnet.selected.vcn_id == var.vcn_ocid
      error_message = "Choose a private subnet in the selected VCN. Existing routing and security lists must be reviewed separately."
    }
    precondition {
      condition     = var.initial_workers <= var.maximum_workers && var.worker_memory_gib >= var.worker_ocpus && var.worker_memory_gib <= var.worker_ocpus * 64
      error_message = "Worker count or memory exceeds the selected deployment envelope."
    }
    precondition {
      condition     = data.oci_core_image.selected.operating_system == "Oracle Linux" && startswith(data.oci_core_image.selected.operating_system_version, "9")
      error_message = "Select an Oracle Linux 9 x86 image compatible with both the coordinator and worker shapes."
    }
    precondition {
      condition     = contains([for ad in data.oci_identity_availability_domains.selected.availability_domains : ad.name], var.availability_domain)
      error_message = "The availability domain is not in the selected tenancy and region."
    }
  }
}

resource "oci_core_network_security_group" "coordinator" {
  depends_on     = [terraform_data.preflight]
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_ocid
  display_name   = "${var.deployment_name}-coordinator"
  freeform_tags  = local.tags
}
resource "oci_core_network_security_group" "workers" {
  depends_on     = [terraform_data.preflight]
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_ocid
  display_name   = "${var.deployment_name}-workers"
  freeform_tags  = local.tags
}
resource "oci_bastion_bastion" "admin" {
  depends_on                   = [terraform_data.preflight]
  bastion_type                 = "STANDARD"
  compartment_id               = var.compartment_ocid
  target_subnet_id             = var.subnet_ocid
  name                         = replace(var.deployment_name, "-", "")
  client_cidr_block_allow_list = [var.admin_cidr]
  max_session_ttl_in_seconds   = 10800
  freeform_tags                = local.tags
}
resource "oci_core_network_security_group_security_rule" "ssh" {
  for_each                  = { coordinator = oci_core_network_security_group.coordinator.id, workers = oci_core_network_security_group.workers.id }
  network_security_group_id = each.value
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "${oci_bastion_bastion.admin.private_endpoint_ip_address}/32"
  source_type               = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}
resource "oci_core_network_security_group_security_rule" "https" {
  for_each                  = { coordinator = oci_core_network_security_group.coordinator.id, workers = oci_core_network_security_group.workers.id }
  network_security_group_id = each.value
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}
resource "oci_core_network_security_group_security_rule" "worker_to_coordinator" {
  for_each                  = toset(["5432", "8443"])
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = oci_core_network_security_group.coordinator.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  tcp_options {
    destination_port_range {
      min = tonumber(each.value)
      max = tonumber(each.value)
    }
  }
}
resource "oci_core_network_security_group_security_rule" "coordinator_from_workers" {
  for_each                  = toset(["5432", "8443"])
  network_security_group_id = oci_core_network_security_group.coordinator.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  tcp_options {
    destination_port_range {
      min = tonumber(each.value)
      max = tonumber(each.value)
    }
  }
}
resource "oci_core_instance" "node" {
  for_each            = local.nodes
  depends_on          = [terraform_data.preflight]
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  display_name        = "${var.deployment_name}-${each.key}"
  shape               = each.key == "coordinator" ? "VM.Standard.E5.Flex" : var.worker_shape
  shape_config {
    ocpus         = each.value.ocpus
    memory_in_gbs = each.value.memory
  }
  create_vnic_details {
    subnet_id        = var.subnet_ocid
    assign_public_ip = false
    nsg_ids          = [each.key == "coordinator" ? oci_core_network_security_group.coordinator.id : oci_core_network_security_group.workers.id]
  }
  source_details {
    source_type             = "image"
    source_id               = var.image_ocid
    boot_volume_size_in_gbs = 50
  }
  instance_options { are_legacy_imds_endpoints_disabled = true }
  metadata      = { ssh_authorized_keys = var.ssh_public_key, user_data = base64encode(file("${path.module}/cloud-init.yaml")) }
  freeform_tags = merge(local.tags, { Role = each.key })
}

resource "oci_kms_vault" "runtime" {
  depends_on     = [terraform_data.preflight]
  compartment_id = var.compartment_ocid
  display_name   = "${var.deployment_name}-runtime"
  vault_type     = "DEFAULT"
  freeform_tags  = local.tags
}
resource "oci_logging_log_group" "runtime" {
  depends_on     = [terraform_data.preflight]
  compartment_id = var.compartment_ocid
  display_name   = "${var.deployment_name}-runtime"
  freeform_tags  = local.tags
}

output "installation_stage" { value = "foundation_only_runtime_not_installed" }
output "configuration_revision" { value = var.profile_revision }
output "nodes" { value = { for name, node in oci_core_instance.node : name => { id = node.id, private_ip = node.private_ip } } }
output "bastion_id" { value = oci_bastion_bastion.admin.id }
output "vault_id" { value = oci_kms_vault.runtime.id }
output "log_group_id" { value = oci_logging_log_group.runtime.id }
