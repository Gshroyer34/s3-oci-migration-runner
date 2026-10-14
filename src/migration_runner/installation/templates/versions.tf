terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    oci = { source = "oracle/oci", version = "8.29.0" }
  }
}

# Resource Manager supplies its own execution identity and state backend.
# Local use requires an operator-configured OCI identity and a separate backend.
provider "oci" {
  region       = var.region
  tenancy_ocid = var.tenancy_ocid
}
