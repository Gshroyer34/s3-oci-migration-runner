# Migration Runner installation foundation

This package prepares private OCI infrastructure and AWS source-read authorization for a new installation. It does not install or start the migration application. Runtime release distribution, enrollment, ledger initialization and UI activation are separate steps provided by the portable installer. Follow the source handoff deployment guide; fresh-tenancy acceptance remains pending. This foundation alone is not a completed installation.

No Codex subscription is required to use these cloud-console templates. Package generation is offline and does not provision resources or validate cloud permissions. The JSON profile contains resource references and an SSH public key, never cloud credentials. Keep tenancy and network details within the intended deployment team.

## Before provisioning

1. Have OCI and AWS administrators confirm the account, tenancy, regions, compartment, source bucket/prefix, encryption key, and destination ownership in `installation-profile.json`.
2. Select an existing private OCI subnet and VCN. The installer does not create or modify existing routing, security lists, DNS, NAT, service gateways, circuits, or S3 endpoints. Their owners must approve service access and networking first. OCI security rules are additive: adding a narrow NSG does not override an overly broad subnet security list.
3. Select an Oracle Linux 9 x86 image compatible with the coordinator E5 shape and chosen worker shape. ARM images and Ampere workers are not supported in this foundation release. Confirm quotas and actual shape capacity.
4. Review compute, boot-volume, network and Vault costs. The worker maximum is a configuration limit, not automatic scaling or a spending cutoff. Assign a resource-retention owner and date.
5. Retain your SSH private key in your own credential store. The public key is the only key allowed in these templates. Use an approved, deployment-owned CA for workload certificates; never reuse the demo CA or put private keys in Terraform, CloudFormation, chat, or the package.

## OCI console installation

1. Extract `oci-foundation.zip` from the outer package. Upload that inner ZIP when creating a new stack in OCI Resource Manager. Do not reuse the demo stack or its Terraform state.
2. Select the intended compartment and supported Terraform version (configuration requires Terraform 1.5 or later, below 2). Review all form values, including the target tenancy, network, administrator CIDR, image and initial worker count.
3. Read the foundation-only acknowledgement. Set it to true only when ready to review provisioning of private hosts, Bastion, security groups, a Vault, and a log group. Until then, the plan deliberately fails its scope check.
4. Run Plan and inspect every resource. This stack must not create, adopt, empty or delete source/destination buckets or existing networking. Review inherited security lists and existing routing independently.
5. Apply the reviewed plan only within the agreed resource budget. Cloud capacity and permission errors must be resolved before retrying. Use the same stack to retry; do not create duplicate installations.
6. Save outputs in the deployment record. Expected stage: `foundation_only_runtime_not_installed`. There is no application URL yet. Bastion access permits the chosen client CIDR and uses your SSH key; sessions are created separately by an authorized operator.

Resource Manager owns this stack's state. Do not copy the lab backend configuration. If running Terraform locally, configure independent protected state and operator authentication before applying. Changing form values after package generation does not regenerate the AWS template or the original profile revision; regenerate the package when changing deployment scope.

## AWS source authorization

1. In the selected source AWS account and region, create a CloudFormation stack using `aws-source-reader.cloudformation.json`.
2. Provide only the public CA certificate for this deployment. Review the IAM permissions and acknowledge IAM resource creation. The template checks the selected account and region.
3. The template creates a Roles Anywhere trust anchor, reader role and profile. It does not create a bucket, modify a bucket policy, grant source writes, upload fixtures, or change retention. Existing bucket policies, KMS key policies, organization controls and endpoint policies must permit the scoped role.
4. Save the reader role, trust anchor and profile ARN outputs, plus the required certificate common name, for the later enrollment stage. Creating these resources alone does not establish a cloud connection.
5. A private-route profile restricts S3 permissions to the configured VPC endpoint. The endpoint must be an S3 interface endpoint reachable from OCI; a gateway endpoint is not a substitute for remote private access. Routing and DNS still require validation. This template does not prove the interconnect exists or provide its bandwidth.

## Verification and activation boundary

`configuration-validation.json` reports syntax and scope checks only. No source listing, destination write, private path, certificate issuance or capacity test has run. The package must not be marked migration-ready until the following runtime and operational stages have been completed and their acceptance checks pass:

- Verified release installation and recoverable ledger initialization.
- Per-node workload enrollment, exact-node Vault policies and independent deployment trust.
- Renewable credentials and CA rollover without dependency on the original developer's workstation.
- Source inventory/read, destination write, encryption and intended-path tests using workload identities.
- Server-side publication of configuration and immutable job binding.
- A bounded, fully verified transfer and restart/recovery test.

The package manifest contains SHA-256 checksums for its files; it detects accidental changes but is not a digital publisher signature. Confirm release provenance through your trusted distribution channel.

## Recovery and teardown

Keep the same stack state and outputs after an interrupted apply. Inspect the failed resource and plan again rather than deleting state. Before teardown, review the destroy plan and any data written after runtime activation. This foundation owns its new hosts, boot volumes, Bastion, security groups, log group and Vault. Existing VCN, subnet, compartment, buckets and encryption keys are references and must remain intact. OCI Vault deletion may involve a service-controlled waiting period.

CloudFormation teardown removes the generated source authorization, not source data. Never use either stack's deletion as a substitute for an approved migration cutover or data-retention decision.

## Provider references

- [OCI Resource Manager schema documents](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm)
- [AWS Roles Anywhere trust model](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html)
- [S3 interface endpoints](https://docs.aws.amazon.com/AmazonS3/latest/userguide/privatelink-interface-endpoints.html)

## S3 tag compatibility notice

The updated worker requires scoped `s3:GetObjectTagging`, included in this AWS
template. Existing deployed reader policies need that change before rollout.
The worker stores tags in OCI metadata `migration-s3-tags-v1`: padded URL-safe
Base64 of a UTF-8 JSON tag map. Case, Unicode and empty values are preserved.
Applications using `GetObjectTagging` must instead read and decode object metadata;
tag writes, tag-based access, lifecycle rules and search are not recreated by the
copy. Review who can read metadata because this can differ from S3 tag permissions.
Copies fail on metadata collisions or the conservative 4,096-byte combined budget
(including 512 bytes reserved for backend fields); tags are never truncated.
Deployment requires the updated worker dependencies and a live pilot. The
complete compatibility and upgrade instructions are in the source release's
`docs/S3_TAG_METADATA_HANDOFF.md`.
