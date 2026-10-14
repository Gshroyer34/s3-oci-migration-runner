# IAM and network completion checklist

These are administrator review inputs for the dedicated evaluation deployment. They are not an automatically applied or fresh-tenancy-qualified policy stack.

## Node identity and Vault

Run `migration-runtime credential-policy` against the final host plan. For each node, create the named dynamic group with the exact `matching_rule` and apply its `steady_state_policy`. Apply the enrollment/update statement only for that node's secret. Set the secret's current plaintext JSON value to the generated `initial_secret_payload`; the Vault interface handles its storage encoding.

Do not create an unrelated broad dynamic group matching every instance in the tenancy. Do not share secrets between nodes. Confirm the secret exists in the intended compartment and uses the selected Vault/key. The administrator creates the key and secrets; runtime nodes only operate on their assigned bundles.

After manual enrollment, remove temporary update access. Managed renewal retains its own-secret update rights and assigned certificate rights. A test must demonstrate denial of cross-node secret and certificate access.

## Destination Object Storage

For a dedicated evaluation bucket, the following **review template** gives one generated node group the permissions needed for object reads/writes and multipart cleanup. Substitute actual group, destination bucket compartment and bucket name; repeat for coordinator and workers:

```text
Allow dynamic-group NODE_GROUP to read buckets in compartment id DESTINATION_BUCKET_COMPARTMENT_OCID where target.bucket.name = 'DESTINATION_BUCKET'
Allow dynamic-group NODE_GROUP to manage objects in compartment id DESTINATION_BUCKET_COMPARTMENT_OCID where all {target.bucket.name = 'DESTINATION_BUCKET', any {request.permission = 'OBJECT_INSPECT', request.permission = 'OBJECT_READ', request.permission = 'OBJECT_CREATE', request.permission = 'OBJECT_OVERWRITE', request.permission = 'OBJECT_DELETE'}}
```

`OBJECT_DELETE` supports aborting incomplete multipart uploads and also permits deletion of objects in this bucket. Keep the bucket dedicated to the trial; the application itself enforces narrower run prefixes. These statements do not make IAM prefix isolation claims. Bucket conditions are case-insensitive, so avoid buckets whose names differ only by case. Bucket read is needed for multipart commit/list operations; this template does not grant bucket creation/deletion. See [Oracle Object Storage permissions](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/objectstoragepolicyreference.htm).

If destination encryption uses a customer-managed key, the Object Storage service also needs the appropriate key permissions; arrange them with the bucket/key administrator. Do not give worker principals key-administrator access as a workaround. The first trial should use a reviewed, uncomplicated destination configuration without retention constraints.

Validate each operation with the actual workload identity. If a backend requests an additional operation, investigate and narrow it deliberately. The supplied runtime config already includes the namespace and addresses the configured bucket; it is not intended to browse all tenancy buckets.

## Managed certificates

The generated `renewal_policies` cover assigned leaf resources and delegates. They do not provision the dedicated roots, signing key, certificate resources or CRL storage/service permissions. Follow [the managed renewal guide](PORTABLE_UI_AND_RENEWAL.md) and the linked Oracle issuer documentation. Record the exact public CA fingerprints and AWS trust anchor, and test first issuance plus renewal before claiming the mode works in this tenancy.

## Network checks

| Flow | Requirement |
| --- | --- |
| Administrator to Bastion | Allowlisted actual public egress CIDR, correct session/key and unexpired session |
| Bastion administrative path to each node | SSH allowed in NSG/security lists and host firewall; verify host key |
| Each worker to coordinator | Private TCP 5432, certificate identity and exact installed node addresses |
| Node to OCI identity/Vault/Object Storage | Approved management/service routes, DNS, HTTPS and instance identity access |
| Node to AWS Roles Anywhere and S3 | Approved internet route for this pilot, correct regions and HTTPS |
| Bootstrap to public repositories | OS packages, Python packages, pinned container registry artifacts and AWS helper |
| Browser to UI | Local tunnel ending at coordinator loopback 8787, separate operator authentication |

OCI rules are additive; a restrictive NSG does not cancel broad security-list permissions. The existing subnet's DNS, routing, gateways and firewall rules are the network owner's responsibility. The template does not build an interconnect, and its optional private planning fields are not runtime route validation.
