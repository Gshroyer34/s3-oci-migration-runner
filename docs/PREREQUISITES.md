# Prerequisites and deployment worksheet

Have an OCI administrator, an AWS administrator and the network owner available for the first installation. One person may fill several roles. The setup assistant runs locally; the runtime requires Linux OCI hosts and root installation access.

## Tools

- Python 3.12 and Git on the preparation workstation. Use the supplied dependency locks.
- A trusted Linux x86_64 build environment with Podman and internet access to the pinned container images, OS/Python repositories and AWS signing-helper download.
- SSH and an approved file-transfer method through your private access path.
- AWS CLI v2 if using the example fixture upload or optional live setup checks; OCI SDK/API-key configuration for optional setup checks. Runtime nodes use OCI instance principals instead.
- OCI Resource Manager and AWS CloudFormation console access. Local Terraform is optional; the templates require Terraform >=1.5 and <2.
- No prebuilt runtime image, offline dependency mirror, cloud accounts or paid-service credits are included.

## Gather before opening the setup form

| Input | Where it comes from |
| --- | --- |
| Deployment name | New lower-case name, 3–24 characters; keep stable |
| Source AWS account ID, region, bucket | AWS administrator; use a dedicated synthetic evaluation bucket |
| Source prefix | Use `fixtures/` with the included generator; it must contain exactly the selected fixtures |
| OCI tenancy and compartment OCIDs, region | OCI administrator |
| Object Storage namespace, destination bucket and prefix | OCI Object Storage; use a dedicated bucket and e.g. `incoming/` |
| Existing VCN and private subnet OCIDs | Network owner; they must be in the selected region/scope |
| Administrator public egress CIDR | The IP/range that reaches Bastion; /24 or narrower, preferably /32 |
| Availability domain and Oracle Linux 9 x86 image OCID | Current region image/shape availability; image must support coordinator E5 and selected workers |
| Worker shape, OCPUs, RAM, initial and maximum workers | Start with one Optimized3 worker, 2 OCPUs/16 GiB; maximum 2 for a small evaluation |
| SSH public key | Deployment-owned OpenSSH RSA or Ed25519 public key; retain the private key privately |
| Credential mode | Manual offline signing or OCI-managed issuers, decided before the host plan |
| Spend/retention owner and end date | Include VM idle time, volumes, requests, storage and AWS outbound traffic |

The foundation creates the coordinator as E5 with 2 OCPUs/16 GB, and each host has a 50 GB boot volume. Confirm quotas and actual shape capacity before applying. The example worker maximum is not a cost cutoff and does not automatically launch more workers.

## Existing infrastructure you must provide

- Existing source and destination buckets; the stacks do not create/adopt/delete them.
- Approved DNS, time synchronization and outbound connectivity. A private subnet still needs an approved route for required public endpoints when using the internet pilot.
- OCI management/Vault/Object Storage access and AWS Roles Anywhere/S3 access from each runtime node. Bootstrap also needs OS/Python/image download access.
- Worker-to-coordinator TCP 5432 through both OCI controls and the host firewall. The template also reserves TCP 8443; this preview's operator UI is loopback 8787, not a public listener on 8443.
- Bastion/SSH access to the private hosts. Do not expose PostgreSQL or the UI directly to the internet.
- Runtime IAM: exact-instance dynamic groups, each node's own Vault secret, destination bucket rights and, in managed mode, certificate resource/issuer dependencies.

## What the cloud stacks do

| Stack | Creates | Does not create |
| --- | --- | --- |
| OCI foundation | Private coordinator/workers, NSGs, Bastion, Vault container, log group | Existing VCN/subnet routes, DNS, gateways, interconnect, buckets, Vault key/secrets, runtime policies, certificate issuers, installed app |
| AWS reader | Roles Anywhere trust anchor, source-reader role and profile | AWS workers, data movers, S3 bucket, source writes, interconnect or S3 private endpoints |

A stack apply is not proof of connectivity. A successful administrator connection test is not proof of worker access. Validate both stages using [the acceptance checklist](FIRST_PILOT.md).
