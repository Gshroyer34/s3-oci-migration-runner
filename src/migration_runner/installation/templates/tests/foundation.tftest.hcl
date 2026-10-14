# Offline provider mocks only. Never use these fabricated identifiers for an apply.
mock_provider "oci" {
  mock_data "oci_core_subnet" {
    defaults = { prohibit_public_ip_on_vnic = true, vcn_id = "ocid1.vcn.oc1..examplealpha0000000000000" }
  }
  mock_data "oci_core_image" {
    defaults = { operating_system = "Oracle Linux", operating_system_version = "9" }
  }
  mock_data "oci_identity_availability_domains" {
    defaults = { availability_domains = [{ name = "example:US-ASHBURN-AD-1" }] }
  }
  mock_resource "oci_bastion_bastion" {
    defaults = { private_endpoint_ip_address = "10.0.0.5" }
  }
}

variables {
  tenancy_ocid                = "ocid1.tenancy.oc1..examplealpha0000000000000"
  compartment_ocid            = "ocid1.compartment.oc1..examplealpha0000000000000"
  region                      = "us-ashburn-1"
  vcn_ocid                    = "ocid1.vcn.oc1..examplealpha0000000000000"
  subnet_ocid                 = "ocid1.subnet.oc1..examplealpha0000000000000"
  image_ocid                  = "ocid1.image.oc1..examplealpha0000000000000"
  availability_domain         = "example:US-ASHBURN-AD-1"
  deployment_name             = "alpha-migration"
  profile_revision            = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ssh_public_key              = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"
  admin_cidr                  = "192.0.2.10/32"
  acknowledge_foundation_only = true
}

run "bounded_private_foundation" {
  command = plan
  assert {
    condition     = length(oci_core_instance.node) == 3 && alltrue([for n in oci_core_instance.node : tostring(n.create_vnic_details[0].assign_public_ip) == "false"])
    error_message = "Expected one coordinator, two private workers and no public IPs."
  }
  assert {
    condition     = output.installation_stage == "foundation_only_runtime_not_installed"
    error_message = "Foundation must not claim runtime readiness."
  }
  assert {
    condition     = oci_bastion_bastion.admin.client_cidr_block_allow_list == tolist(["192.0.2.10/32"])
    error_message = "Bastion access must stay inside the administrator range."
  }
}

run "different_deployment_and_worker_count" {
  command = plan
  variables {
    deployment_name   = "bravo-migration"
    initial_workers   = 1
    worker_shape      = "VM.Standard.E4.Flex"
    worker_ocpus      = 4
    worker_memory_gib = 32
  }
  assert {
    condition     = length(oci_core_instance.node) == 2 && oci_core_instance.node["worker-1"].display_name == "bravo-migration-worker-1" && oci_core_instance.node["worker-1"].shape_config[0].ocpus == 4
    error_message = "New deployment values must drive names, worker count and sizing."
  }
}

run "public_subnet_rejected" {
  command = plan
  override_data {
    target = data.oci_core_subnet.selected
    values = { prohibit_public_ip_on_vnic = false, vcn_id = "ocid1.vcn.oc1..examplealpha0000000000000" }
  }
  expect_failures = [terraform_data.preflight]
}

run "wrong_vcn_rejected" {
  command = plan
  variables { vcn_ocid = "ocid1.vcn.oc1..othernetwork000000000000" }
  expect_failures = [terraform_data.preflight]
}

run "worker_envelope_rejected" {
  command = plan
  variables { maximum_workers = 1 }
  expect_failures = [terraform_data.preflight]
}

run "foundation_acknowledgement_required" {
  command = plan
  variables { acknowledge_foundation_only = false }
  expect_failures = [terraform_data.preflight]
}

run "broad_admin_access_rejected" {
  command = plan
  variables { admin_cidr = "0.0.0.0/0" }
  expect_failures = [var.admin_cidr]
}
