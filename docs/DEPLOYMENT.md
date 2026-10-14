# Deploy in your own OCI tenancy

Follow these stages in order. Commands run from the repository root unless a node or private workspace is named. Replace upper-case placeholders; do not run them literally. Use a new deployment and synthetic data. No Codex is required.

The setup assistant prepares templates. Administrators still apply infrastructure, complete IAM, build the runtime image, transfer files and install each node. The [base installer reference](PORTABLE_INSTALLER.md) contains detailed guards; [managed renewal reference](PORTABLE_UI_AND_RENEWAL.md) defines the optional issuer contract.

## 1 Prepare a private deployment workspace

Complete the [worksheet](PREREQUISITES.md), verify the handoff and install the Python environment using the root README. Create a private directory **outside the clone** for configuration, AWS stack outputs, public certificates, credentials and generated releases. On Linux use `umask 077` before creating it; on Windows restrict its ACL to the operator.

Keep the environment activated. `migration-install` is equivalent to `python -m migration_runner.installation`; `migration-runtime` is equivalent to `python -m migration_runner.installation.host_cli`.

Start the setup assistant:

```sh
python -m migration_runner.installation serve
```

Fill in the worksheet values and export the configuration/package. Select **internet** for this runtime preview and omit a KMS key. The private-route fields in the setup form are planning/foundation inputs, not enabled portable runtime support.

Alternatively, copy `examples/installation-profile.example.json` into your private workspace, replace every placeholder and run:

```sh
migration-install validate --profile /PRIVATE/installation-profile.json
migration-install package --profile /PRIVATE/installation-profile.json --output /PRIVATE/foundation.zip
```

Expected: configuration validation and a ZIP, with no cloud changes. Never put AWS secrets or OCI private keys into this JSON.

Optional read-only local connection stage, using your own existing CLI/SDK profiles:

```sh
migration-install serve --live-connections --aws-profile YOUR_AWS_PROFILE --oci-profile YOUR_OCI_PROFILE
```

This checks selected cloud APIs from the workstation. It does not prove payload writes, multipart behavior, worker identity, a private route or available bandwidth. A VPN can affect these requests.

## 2 Prepare synthetic data and choose credential mode

Follow [fixture preparation](FIRST_PILOT.md) to generate 3 × 1 MB local files and an independent `reference.json`. Upload only the `.bin` files to your dedicated source bucket's `fixtures/` prefix. Keep the reference local. Source objects, metadata and tags must remain unchanged throughout the trial.

Choose one credential mode before creating AWS trust:

**Manual signing:** on the secure preparation workstation:

```sh
migration-runtime create-authorities --name YOUR_DEPLOYMENT_NAME --output /PRIVATE/authorities
migration-runtime create-token --output /PRIVATE/worker-token
```

Enter the CA passphrase only at the terminal prompt. Protect encrypted CA keys and the token. This creates separate transport and AWS roles authorities; leaves last at most 24 hours, these offline authorities 30 days. Renewal requires another signing session.

**OCI-managed renewal:** provision two dedicated self-signed OCI root CAs and their required signing-key/CRL/IAM resources. Export only their public certificates to `/PRIVATE/authorities/tls-ca.crt` and `roles-ca.crt`. Generate the worker token with `create-token` as above. Do not run `create-authorities` in that directory. The [managed renewal guide](PORTABLE_UI_AND_RENEWAL.md) lists certificate profiles, subject names, SAN and permissions. Issuer provisioning remains a manual administrator task; no CA keys go on runtime nodes.

Both modes support the portable UI. Managed mode cannot reuse the manual-signing private CAs. Do not switch modes midway through an installation.

## 3 Apply the two foundation stacks

Extract `/PRIVATE/foundation.zip` into its own private directory.

1. Upload its **inner** `oci-foundation.zip` to a new OCI Resource Manager stack. Review the input values. Acknowledge that this creates billable foundation resources only; Plan, inspect, then Apply. Preserve the stack state.
2. Save outputs: `nodes` (instance IDs and private IPs), `bastion_id`, `vault_id`, `configuration_revision` and `log_group_id`.
3. In the intended AWS account/region, create a CloudFormation stack from `aws-source-reader.cloudformation.json`. Supply the public **roles CA certificate**, not the transport CA or a private key. Review the IAM creation acknowledgement.
4. Save `ReaderRoleArn`, `TrustAnchorArn`, `ReaderProfileArn` and `CertificateCommonName`. Place the first three into the keys shown in `examples/reader-identity.example.json`.

The AWS stack grants scoped source reads including tags and ACL observations. Bucket policies, organizational restrictions and endpoint policies can still deny access. It does not modify them. Nothing in these stages connects two cloud networks or starts a migration.

Existing generated templates may describe runtime installation as unfinished: that text describes the foundation component. The separate portable installer is included here, with fresh-cloud acceptance pending. Follow this guide for the combined release.

## 4 Complete access and credential resources

Create Bastion sessions for administrative SSH to the new hosts. Use the OCI console's generated SSH commands for your session and public key. Check host image compatibility, time synchronization and the approved outbound paths before bootstrap.

In the foundation Vault, create a symmetric encryption key and a separate secret for the coordinator and every worker. Begin with nonsecret placeholder contents so you can obtain their OCIDs. After generating the host plan in stage 6, replace each placeholder with its exact `initial_secret_payload` from `credential-policy.json` before enrollment. Do this through the approved Vault interface; never put real credential bundles into Terraform state.

Use the concrete [IAM and network checklist](IAM_AND_NETWORK.md), including the destination policy review template. OCI IAM must cover:

- Each node's exact-instance dynamic group and only its assigned secret. Generated statements arrive in stage 6.
- Destination bucket access using instance principals on coordinator/workers: the runtime checks listing/HEAD and upload behavior, including multipart operations. Have the OCI administrator grant and test the required bucket/object permissions scoped to the dedicated evaluation bucket. **No complete destination IAM stack is included.** Do not compensate with tenancy-wide administration.
- Both NSG and host-firewall access from each worker's stable private IP to coordinator TCP 5432; SSH through the chosen administrative route.
- In managed mode, each preassigned certificate and dedicated issuer's required key/CRL/delegate permissions. The generated leaf policies alone are not a complete issuer setup.

Managed mode also requires three certificate resources for the coordinator (`tls`, `roles`, `database`) and two per worker (`tls`, `roles`). Precreate them following the renewal reference, using bootstrap CSRs if necessary. The node's first renewal replaces the public certificate with one matching its locally generated key. Bootstrap private keys never become runtime credentials.

## 5 Build the runtime image on Linux x86_64

Use the exact reviewed source tree and locked dependencies:

```sh
podman build -f Containerfile -t localhost/migration-runner:0.5.0 .
podman build -f Containerfile.cloud --build-arg BASE_IMAGE=localhost/migration-runner:0.5.0 -t localhost/migration-portable:preview-2 .
podman image inspect --format '{{.Id}}' localhost/migration-portable:preview-2
podman save -o /PRIVATE/runtime-image.tar localhost/migration-portable:preview-2
sha256sum /PRIVATE/runtime-image.tar
```

Record two different values: the **image ID** (`sha256:HEX`) used by the host plan, and the **image archive checksum** used to verify file transfer. If Podman prints the image ID without `sha256:`, add that prefix for the plan. This image includes the UI, worker code, Rclone and pinned AWS signing helper. There is no separate UI image to build for portable mode.

The supplied Python wheel is useful for package installation; it does not include the OS, Rclone, database or signing-helper binaries and is not a substitute for this container build. Dependencies still require repository access.

## 6 Generate the immutable runtime and host plans

Create the deployment package from the same installation profile and AWS outputs:

```sh
migration-install runtime-package --profile /PRIVATE/installation-profile.json --reader-identity /PRIVATE/reader-identity.json --destination-run-prefix incoming/pilot-001/ --output /PRIVATE/pilot-profile.zip
```

The destination run prefix must be strictly below your approved destination prefix. Extract `deployment-profile.json` and `binding.json` into `/PRIVATE/pilot-profile/`. Copy `examples/nodes.example.json` into your private workspace and fill it with the actual instance IDs, private IPs and assigned secret OCIDs. Node IDs must be `coordinator`, `worker-1`, and consecutively numbered additional workers.

For UI/manual signing, use `examples/features-ui.example.json`. For UI/managed renewal use `examples/features-managed-one-worker.example.json` and extend it for every additional worker. Replace `reference_sha256` with the SHA-256 of the exact local `reference.json` file. Select the lifetime UI run allowance (default 3; allowed 1–10) before building the plan.

```sh
migration-runtime make-plan --deployment /PRIVATE/pilot-profile/deployment-profile.json --nodes /PRIVATE/nodes.json --runtime-image sha256:REVIEWED_IMAGE_ID --authorities /PRIVATE/authorities --token-file /PRIVATE/worker-token --features /PRIVATE/features.json --output /PRIVATE/host-plan.json
migration-runtime validate-plan --plan /PRIVATE/host-plan.json
migration-runtime credential-policy --plan /PRIVATE/host-plan.json --output /PRIVATE/credential-policy.json
migration-runtime build-release --plan /PRIVATE/host-plan.json --authorities /PRIVATE/authorities --source-root /PATH/TO/CLONE --output /PRIVATE/host-release.zip
```

Record the returned host-release checksum. Review and apply the generated exact-node IAM statements and exact placeholder payloads now. In manual mode secret-update permissions are temporary; in managed mode they are retained. Wait for permission propagation and verify isolation.

## 7 Install and enroll every node

Transfer the source installer TAR from `packages/`, `runtime-image.tar`, `host-release.zip` and their separately verified checksums to each host. Do not transfer the authorities directory, CA keys, workstation cloud profiles or Terraform state. The deployment worker token is transferred separately into a root-owned mode-0600 file for enrollment.

On each host, verify the archive checksums first, extract the source into an administrator-owned directory, then:

```sh
cd /ROOT_OWNED_PATH/migration-installer
sudo bash deploy/install-portable.sh
sudo podman load -i /PRIVATE/runtime-image.tar
sudo /opt/migration-installer/bin/migration-runtime install --release /PRIVATE/host-release.zip --expected-sha256 REVIEWED_HOST_RELEASE_SHA256 --node coordinator
```

Use that host's `worker-1`, etc. node ID on workers. Expected: installation checkpoint with no migration or database initialization. `install` is repeatable with the same release; the OS/Python bootstrap is a one-time step.

**Manual mode:** run `request --output /root/node-request.json` on each node, transfer that public request to the signing workstation, run `sign --plan ... --request ... --authorities ... --output ...`, return its signed public response, then run `enroll --signed ... --token-file /root/worker-token` on that node. Exact commands and checks are in [base enrollment](PORTABLE_INSTALLER.md). Remove temporary enrollment update permissions after successful read-back, keeping secret-bundle read rights.

**Managed mode:** run the following on each node; repeat after issuance processing if it reports `issuance_pending`:

```sh
sudo /opt/migration-installer/bin/migration-runtime renew --token-file /root/worker-token
```

Require `enrolled: true`. Keep the managed renewal update permissions. In both modes, remove the temporary token transfer files after enrollment and preserve the authoritative token in your approved private credential store.

## 8 Initialize and open the private UI

On the coordinator only:

```sh
sudo /opt/migration-installer/bin/migration-runtime initialize-ledger
sudo /opt/migration-installer/bin/migration-runtime status
sudo /opt/migration-installer/bin/migration-runtime activate-ui --reference /root/reference.json
```

On each worker:

```sh
sudo /opt/migration-installer/bin/migration-runtime activate-ui
```

Retrieve the coordinator's operator token from `/var/lib/migration-portable/operator-token` through your private administrator channel. Do not use the worker token to sign in. The activation command does not print it.

The UI listens on **coordinator loopback** `127.0.0.1:8787`. Create an SSH connection to the coordinator through Bastion and add local forwarding `-L 8787:127.0.0.1:8787` to that coordinator SSH connection. The forwarding destination must be resolved from the coordinator SSH server; a direct Bastion TCP forward to the coordinator's private IP port 8787 cannot reach a loopback-only listener. Keep `ExitOnForwardFailure=yes` and normal SSH host-key checking. Use the console-generated Bastion proxy/session command rather than guessing a session hostname.

Open `http://127.0.0.1:8787` locally, enter the operator token, confirm source/target scope and worker readiness. Port 8793 is the local setup assistant; port 8787 is the deployed dashboard.

Oracle provides [Bastion connection instructions](https://docs.oracle.com/en-us/iaas/Content/Bastion/Tasks/connectingtosessions.htm). Sessions expire separately from application certificates; renewing one does not renew the other.

## 9 Perform acceptance and record the result

Use [the first-pilot checklist](FIRST_PILOT.md). Installation is not accepted until workload identities can read/write the approved scope, the UI runs a bounded verified migration, and restart/credential handling are demonstrated. Record the image ID, source checksum, deployment revision, node IDs, job ID, manifest and unresolved observations in your private deployment record.

Do not delete a ledger or weaken scope checks to bypass an installation failure. Read [operations](OPERATIONS.md) and preserve checkpoints for diagnosis.
