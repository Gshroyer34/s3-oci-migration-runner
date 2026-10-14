# Portable OCI installer and credential enrollment

Engineering preview · October 14, 2026 · No Codex requirement

**Preview 2 update:** the [portable UI and unattended leaf renewal](PORTABLE_UI_AND_RENEWAL.md)
are now implemented as optional, reviewed host-plan features. Follow that guide
alongside this base installation procedure. The original manual/CLI path below
still works. Fresh OCI acceptance remains pending for both paths.

## What this release delivers

An administrator can install the pilot runtime on fresh Oracle Linux 9 x86_64
OCI instances, enroll deployment-owned credentials, initialize a private ledger,
and explicitly run a bounded pilot from the command line. The installer verifies
a reviewed release checksum, pins the runtime image, validates the actual OCI
instance against its node plan, installs systemd services, and records checkpoints.
It never starts a migration during installation or enrollment.

**This is not yet a production migration release.** The base path uses the scoped
CLI; preview 2 adds a separate portable UI and dispatch service. The supported
envelope remains immutable synthetic data, STANDARD
and SSE-S3, internet routing, at most 100 objects / 10 GB / 100 MB per object / four
workers / one hour, with full source/destination SHA-256 and an independent fixture
manifest. A profile can narrow these limits. Private interconnect operation,
750 TB inventory, Glacier Instant Retrieval admission, automatic scaling, SSO,
production PKI lifecycle management and CA rollover remain separate work.

No changes to the running lab are required. This installer explicitly refuses a
host with the existing lab image marker. A different plan or release cannot be
installed over an existing portable installation; upgrades need a separate,
reviewed procedure.

## Files and ownership

| Location | Purpose |
|---|---|
| `/opt/migration-installer` | Root-owned Python environment and installer commands |
| `/etc/migration-portable` | Immutable plan, public trust anchors, schema, receipt and database configuration |
| `/var/lib/migration-portable` | Checkpoints, ledger storage, enrollment requests/leaf keys and file exchange |
| `/run/migration-portable` | Restored credentials on tmpfs; lost on reboot and recovered from Vault |
| `migration-portable-db.service` | Coordinator database, automatically restored from Vault on service start |
| `migration-portable-worker@JOB_ID.service` | Worker for an explicitly selected, admitted pilot; not enabled automatically |

Private leaf keys are generated on each node. They are retained in the root-only
enrollment directory to make retries and manual renewal possible, and encrypted
at rest by OCI Vault after enrollment. They also inherit the boot volume's
encryption while retained on disk. This is **not** a design in which private keys
exist only in memory. Secure the node's root access and boot-volume backups.
In manual-signing mode, the two CA private keys stay on the signing workstation, encrypted with a
passphrase; they never go to OCI nodes, Terraform, CloudFormation or the archive.

The existing worker authentication model uses one high-entropy deployment token,
plus separate node TLS certificates. The database stores only the token hash.
Workers have scoped SQL permissions but remain trusted execution principals;
they are not isolated against a malicious enrolled worker. The original lab
token, API keys and operator login are never reused.

## 1. Prepare the infrastructure and reviewed configuration

Use the existing setup assistant or `migration-install` commands to generate the
OCI foundation and AWS reader stack. Review/apply them in the intended accounts.
The foundation provisions private compute, network security groups, Bastion, a
Vault and a log group. It does not create a circuit or configure S3 private DNS.

For this runtime preview, use an internet-route portable deployment profile:

```text
python -m migration_runner.installation runtime-package --profile installation-profile.json --reader-identity reader-identity.json --destination-run-prefix incoming/pilot-001/ --output pilot-profile.zip
```

Extract `deployment-profile.json` and `binding.json`. AWS reader identity fields
must come from that deployment's AWS stack outputs. The AWS stack must trust the
**roles CA public certificate created below**, with the deployment-specific
certificate subject required by the generated template. It grants source reads,
tag reads and ACL observations; it does not provision AWS workers or move data.

Have the administrator supply:

- The actual coordinator and worker instance OCIDs and private IPv4 addresses,
  all in the destination compartment and region. Use one to four workers, within
  the approved profile. Host IP addresses must be stable.
- A symmetric encryption key in the foundation Vault and a separate secret for
  each node. Create placeholders first to obtain their OCIDs; after building the
  plan, replace them with the exact nonsecret enrollment placeholders generated
  by `credential-policy`. No credential payload belongs in Terraform state.
- Exact-instance dynamic groups and exact-secret permissions from that output.
  The temporary enrollment policy grants only secret metadata read/update on that
  node's secret. Remove it after successful read-back. Steady-state access reads
  that one secret bundle. Do not grant workers access to another node's secret.
- Destination bucket permissions for the chosen source-to-target mapping,
  including the Rclone list/HEAD/create/multipart operations already required by
  the runtime. This installer does not apply object-storage IAM.
- NSG **and host firewall** access from each listed worker IP to coordinator TCP
  5432, and private administrator access through Bastion. The installer restricts
  PostgreSQL to the coordinator IP and exact client IPs; it does not edit the host
  firewall or existing security lists. No public database listener is needed.
- Outbound management access to OCI instance identity, Vault, Object Storage,
  AWS Roles Anywhere and S3, plus the OS/Python/image repositories used during
  bootstrap. Accurate time synchronization is required.

Oracle documents the Vault [permission types](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/keypolicyreference.htm)
and [conditional secret updates](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/vault/secret/update.html).
The generated policies have local contract tests; their application in a fresh
tenancy still needs acceptance testing.

## 2. Build and distribute a trusted release

The current engineering handoff is `packages/migration-installer-handoff-preview-2.tar`.
Its adjacent SHA-256 receipt must be delivered over a trusted channel. The source
archive includes the installer, locked dependency versions, infrastructure
templates, migrations, build recipes and this guide. It contains **no** node plan,
credentials, fixture data, runtime image, cloud state or customer identities.

On a trusted Linux build machine, extract it and build the runtime images:

```sh
podman build -f Containerfile -t localhost/migration-runner:0.5.0 .
podman build -f Containerfile.cloud --build-arg BASE_IMAGE=localhost/migration-runner:0.5.0 -t localhost/migration-portable:preview-2 .
podman image inspect --format '{{.Id}}' localhost/migration-portable:preview-2
podman save -o runtime-image.tar localhost/migration-portable:preview-2
sha256sum runtime-image.tar
```

Record the full image ID as `sha256:HEX` (some Podman versions display just the
hexadecimal part). Distribute the image archive and its independently verified
checksum. Version pins and checksums do not establish publisher identity by
themselves. The bootstrap currently needs configured package repository access;
there is no offline wheelhouse in this release.

On the secure signing workstation, install the supplied Python package/dependencies
in a Python 3.12 environment. Use a private directory accessible only to the
operator. Commands below use `migration-runtime`; equivalent invocation is
`python -m migration_runner.installation.host_cli`.

```text
migration-runtime create-authorities --name DEPLOYMENT_NAME --output authorities
migration-runtime create-token --output worker-token
```

Enter the CA passphrase at the terminal prompts, never in a command argument or
chat. Back up the encrypted CA keys and passphrase separately. Authorities last
30 days; leaf certificates last at most 24 hours. This is a pilot lifecycle,
not a production availability commitment.

Create `nodes.json`, using actual stack outputs. This example is structural only:

```json
[
  {"node_id":"coordinator","instance_id":"INSTANCE_OCID","private_ip":"10.20.1.10","secret_id":"COORDINATOR_SECRET_OCID"},
  {"node_id":"worker-1","instance_id":"INSTANCE_OCID","private_ip":"10.20.1.20","secret_id":"WORKER_SECRET_OCID"}
]
```

```text
migration-runtime make-plan --deployment deployment-profile.json --nodes nodes.json --runtime-image sha256:REVIEWED_IMAGE_ID --authorities authorities --token-file worker-token --output host-plan.json
migration-runtime credential-policy --plan host-plan.json --output credential-policy.json
migration-runtime build-release --plan host-plan.json --authorities authorities --source-root PATH_TO_EXTRACTED_SOURCE --output host-release.zip
```

Record the returned release SHA-256 independently. Review the plan, apply the
exact-node policies, and set each secret's generated placeholder **before** node
enrollment. Copy only `host-release.zip`, the source installer and runtime image
to nodes. Keep the authorities directory on the signing workstation.

## 3. Install each fresh OCI node

Verify the source and image archive SHA-256 values before extraction/import.
Run the supplied bootstrap from the root-owned extracted source directory:

```sh
sudo bash deploy/install-portable.sh
sudo podman load -i runtime-image.tar
sudo /opt/migration-installer/bin/migration-runtime install --release host-release.zip --expected-sha256 REVIEWED_RELEASE_SHA256 --node coordinator
```

Use `--node worker-1` on that worker, and the corresponding ID on other workers.
The installer checks the actual instance OCID, compartment, region and local IP;
a release for another node is rejected. It checks the loaded runtime image ID,
imports public profile/trust files, and installs service definitions. On the
coordinator it pulls the digest-pinned PostgreSQL 17.11 image. **No service starts
and no database is initialized at this stage.**

Rerunning `install` with the identical release and node is safe. It checks existing
files and keeps the checkpoint. Changed files, another plan/release/node, a
symbolic link, writable authority directories or an unmanaged ledger stop the
operation. The bootstrap shell creates its Python environment once; if dependency
installation fails, inspect the partial environment and complete the listed pip
steps manually. Do not delete runtime data to retry a toolchain installation.

## 4. Request, sign and enroll credentials

On each node:

```sh
sudo /opt/migration-installer/bin/migration-runtime request --output /root/node-request.json
```

Copy this **public request** to the signing workstation. Review its node ID and
plan revision, then sign it there:

```text
migration-runtime sign --plan host-plan.json --request node-request.json --authorities authorities --output node-signed.json
```

Return the signed public certificates to that same node. Transfer the deployment
token using your approved secure administrative channel into a root-owned 0600
file, such as `/root/worker-token`. Do not paste the token into arguments, tickets,
logs or Terraform variables. On that node:

```sh
sudo /opt/migration-installer/bin/migration-runtime enroll --signed node-signed.json --token-file /root/worker-token
```

Enrollment validates pinned CA fingerprints, signature, subject, certificate
purpose, node-local key match, validity, database address and token hash. It uses
the instance's identity to update only its assigned placeholder secret, checks
the current version and ETag to detect concurrent changes, and reads it back.
Retrying identical credentials does not create another version. An unrelated
existing secret is refused. Keep the returned nonsecret credential digest.

After all read-backs pass, remove the temporary secret-update policies and the
temporary token transfer files. Keep the narrowly scoped secret-bundle read
policies. Use `restore` on each stopped node to confirm it still reads its secret
with steady-state permissions. Negative-test reading another node's secret.

## 5. Initialize the coordinator exactly once

```sh
sudo /opt/migration-installer/bin/migration-runtime initialize-ledger
sudo /opt/migration-installer/bin/migration-runtime status
```

The database service restores its TLS material from Vault before startup. The
installer initializes only storage owned by this installation. Schema, scoped
database roles, deployment identity and token hash are committed together. A
successful retry verifies the existing identity/schema/token rather than replacing
them. An unknown database or partially initialized storage is preserved and the
operation stops for inspection. There is no automatic reset, drop or upgrade.

The database service is enabled for boot only after bootstrap succeeds. Worker
services stay inactive. `status` reports the local checkpoint and credential
expiry; it is **not** a cloud connectivity or migration readiness certification.

## 6. Run an explicitly approved pilot

After network, IAM, source freeze and exclusive destination checks, put the
reviewed `binding.json` and independent `fixture-reference.json` in the
coordinator's `/var/lib/migration-portable/exchange` directory (owner 10001, mode
0700; input files readable by 10001). Containers see this directory as `/exchange`.
Create the directory with those permissions before the first command if needed.

```sh
sudo /opt/migration-installer/bin/migration-runtime execute -- validate --binding /exchange/binding.json
sudo /opt/migration-installer/bin/migration-runtime execute -- inventory-plan --binding /exchange/binding.json --reference-manifest /exchange/fixture-reference.json
sudo /opt/migration-installer/bin/migration-runtime execute -- start --binding /exchange/binding.json --job REVIEWED_JOB_UUID --max-workers 1 --max-seconds 3600 --max-scheduled-bytes REVIEWED_BYTE_ALLOWANCE
```

`validate` is offline scope validation. `inventory-plan` probes access and builds
the bounded plan; neither proves a private network path. Inspect the returned
inventory/job report before `start`. On each approved worker, run:

```sh
sudo systemctl start migration-portable-worker@REVIEWED_JOB_UUID.service
```

Workers restore their own credentials, authenticate to the ledger with TLS and the
deployment token, and reject jobs outside their independently installed profile.
They exit on completion, cancellation, drain or bounded failure. They are not
automatically restarted or scheduled onto another job. Do not enable these
instance units at boot. From the coordinator:

```sh
sudo /opt/migration-installer/bin/migration-runtime execute -- status --job REVIEWED_JOB_UUID
sudo /opt/migration-installer/bin/migration-runtime execute -- drain --job REVIEWED_JOB_UUID
sudo /opt/migration-installer/bin/migration-runtime execute -- export-manifest --job REVIEWED_JOB_UUID --output /exchange/manifest.zip
```

Migration runtime checks and the ledger remain authoritative for worker counts,
byte/time limits and scope. Installation does not enlarge those limits or admit
production objects. Review the S3 tag and property compatibility guides included
in the archive; applications must change tag API calls, and ownership, lifecycle,
ACLs and retention are not recreated as OCI object policies.

## Renewal, interrupted operations and recovery

- Before the 24-hour leaf expiry, repeat `request` to a new output file, sign it,
  and `enroll --replace-digest PREVIOUS_CREDENTIAL_DIGEST`. The existing node-local
  key is reused; renewal never silently replaces unknown secret contents. Temporarily
  restore that node's enrollment IAM policy and remove it after read-back.
- Drain active work before switching mounted credentials. Stop the affected
  worker services/client commands; on the coordinator, stop the database too.
  Run `restore`, then restart the database and explicitly resume the approved job.
  Restoring while a portable container is active is refused. An expired certificate
  cannot be restored; sign and enroll a valid leaf first.
- A reboot loses tmpfs credentials. The coordinator restores from Vault on DB
  start; a worker restores when an explicitly selected worker unit starts. Failure
  to obtain/validate credentials stops startup. There is no interactive passphrase
  requirement on nodes because authority keys are never installed there.
- Public CA rollover and trust-anchor replacement are **not automated**. Schedule
  the pilot within the authority validity window. Do not distribute these 30-day
  authorities as an unattended production PKI design.
- A failed or interrupted Vault write can be retried with the same signed response
  and local keys. If the new content already became CURRENT, read-back completes
  without another version. A changed current version requires investigation.
- Preserve `/var/lib/migration-portable/postgres` and the installation receipts.
  An incomplete `initdb` is intentionally not erased automatically. Backups,
  restore drills and upgrades are not implemented by this preview.
- Logs and CLI errors suppress provider payloads and credentials. The checkpoint
  identifies the last completed phase. Inspect systemd health and configuration
  locally; do not paste secret files into support requests.

## Qualification and next acceptance gate

The [qualification record](benchmarks/portable-installer-20261014.json) records 31
distinct new tests passed across Windows, Linux and isolated PostgreSQL. Local
tests cover enrollment, signing, pinning, wrong identity/key/CA/purpose,
expired leaves, exact-secret policies, read-back and concurrency failures,
idempotency, archive tampering, actual Linux ownership and symlink checks,
tmpfs preparation, database protection and generated services. Database schema
and permission rehearsal passed against real PostgreSQL 17.11. Host service
orchestration, OCI metadata and Vault calls use test substitutes; those boundaries
have not been qualified by these tests on a fresh cloud host.

Still required before calling this customer-ready: execute the guide on fresh
OCI nodes and the recipient's AWS reader stack; verify IAM propagation and
cross-node denial; check reboot/renewal; perform a small full pilot; confirm the
private route separately when supported; and accept the new portable UI against
real hosts. No fresh-tenancy provisioning, IAM change,
live migration or production rollout was performed by this engineering step.
