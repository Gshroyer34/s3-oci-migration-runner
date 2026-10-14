# Portable UI and unattended certificate renewal

Engineering preview 2 · October 14, 2026

## Delivered behavior

The portable coordinator now has its own operator UI. It displays the installed
source/destination, route, limits and certificate state; creates a bounded preview;
starts one to four installed workers; pauses, drains, resumes and cancels runs;
and exports the migration manifest. The source/destination/identity settings are
administrator-approved installation inputs, not arbitrary fields a job can
override. The operator chooses a worker count within those inputs.

The UI binds only to `127.0.0.1:8787` on the coordinator and requires a separate
operator token through an authenticated SSH/Bastion tunnel. The token stays in
browser memory. This release does not add public HTTPS, SSO or multiple operator
roles. Worker dispatch is a node-local timer reading the authenticated ledger;
the browser cannot submit shell commands or instance provisioning requests.

Unattended **leaf certificate** renewal uses OCI Certificates issuers. Each node
retains its own private keys and requests public certificates for its preassigned
certificate resources. No CA private key or passphrase is installed on workers or
the coordinator. Node identities use OCI instance principals, so renewal does not
depend on an unexpired TLS client certificate or an administrator laptop.

The original offline signing workflow remains available when managed renewal is
not configured. **Managed renewal cannot sign using the old offline CA keys.** A
fresh managed-mode installation needs its own OCI issuers and matching AWS trust.

## Prerequisites and approval boundaries

Use the [portable installation guide](PORTABLE_INSTALLER.md) for the base
infrastructure and source/image build. Preview 2 requires schema
`0007_portable_workers`, a matching rebuilt runtime image and fresh host plans.
It does not upgrade the existing lab or a preview-1 installation automatically.

An administrator provisions two dedicated, self-signed OCI root CAs: one for
transport/database certificates, one for AWS Roles Anywhere certificates. Configure
their signing keys, issuer policies and CRL resources following the OCI service
requirements. Do not reuse a general corporate authority for this preview. The
runtime currently validates direct leaf-to-root issuance; intermediate chains
are not supported by this implementation.

Download the two **public** CA PEM files as `tls-ca.crt` and `roles-ca.crt` into the
authorities input directory used by `make-plan` and `build-release`. This directory
needs no CA private keys for managed mode. Configure AWS Roles Anywhere to trust
the roles CA public certificate and the generated deployment-specific subject.

Precreate one externally managed certificate resource per node and purpose:

| Node | Purpose | OCI profile | Expected common name |
|---|---|---|---|
| Coordinator | `tls` | `TLS_CLIENT` | `migration-NAME-coordinator` |
| Each worker | `tls` | `TLS_CLIENT` | `migration-NAME-worker-N` |
| Every node | `roles` | `TLS_CLIENT` | `migration-NAME-worker` |
| Coordinator | `database` | `TLS_SERVER` | `migration-NAME-database` |

The database CSR must contain exactly the coordinator's private IPv4 address as
its SAN. Certificate configuration type must be
`MANAGED_EXTERNALLY_ISSUED_BY_INTERNAL_CA`. The initial resources can use temporary
administrator-generated bootstrap keys/CSRs with these same identities; those
keys never become runtime node keys. The initial node renewal replaces their
public certificates with ones matching keys generated locally on each node.
Retire temporary bootstrap keys after enrollment. Issuers/resource setup is still
an administrator step, not an installer-applied infrastructure stack.

The service APIs support submitting CSRs for externally held keys, while our
timer supplies the scheduling and activation logic. See Oracle's
[certificate PEM update workflow](https://docs.oracle.com/en-us/iaas/Content/certificates/updating-certificate-PEM.htm)
and [Certificates IAM reference](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/certificatespolicyreference.htm).

## Build a feature-enabled host plan

Prepare a nonsecret `features.json` with:

```json
{
  "ui": {
    "reference_sha256": "SHA256_OF_THE_REVIEWED_REFERENCE_JSON",
    "max_runs": 3
  },
  "managed_certificates": [
    {
      "node_id": "coordinator",
      "purpose": "tls",
      "certificate_id": "COORDINATOR_TLS_CERTIFICATE_OCID",
      "authority_id": "TRANSPORT_CA_OCID"
    }
  ]
}
```

The example shows one resource entry only. Supply every entry from the table:
three for the coordinator and two for each worker. The validator rejects missing
entries, duplicate resources, a shared transport/AWS CA, unknown nodes and
nonconsecutive UI worker IDs. Use `worker-1` through `worker-N`.

Pass `--features features.json` to the existing `make-plan` command. The reference
hash pins the independent synthetic fixture manifest, not an arbitrary inventory
uploaded through the browser. `max_runs` is the lifetime UI admission allowance
for this installed plan, from 1 to 10; it is not a daily reset. Existing pilot
limits remain unchanged, including 100 MB maximum object size and full SHA-256.

Generate `credential-policy` output from the final plan. Managed renewal needs
**ongoing**, exact-node secret metadata read/update and secret-bundle read, plus
read/update on its assigned certificate resources and public-only certificate
bundle access. The output marks these rights as retained for managed mode. Do
not remove them using the manual-enrollment-only cleanup instructions.

The generated leaf/delegate statements are only part of the issuer permissions.
The administrator must complete and verify OCI's required CA/key/CRL dependencies,
using dedicated resources and least privilege, including appropriate subject
restrictions. No `manage all-resources` policy is needed or generated. A negative
test must prove that each node cannot read or update another node's secret or
certificate resources. Workers remain trusted migration execution principals;
the application checks are not a substitute for provider-side IAM isolation.

## Enroll and activate

Install the final reviewed release on each fresh host. For first managed
enrollment, transfer the deployment token through the approved private channel
into a root-owned 0600 file, then run:

```sh
sudo /opt/migration-installer/bin/migration-runtime renew --token-file /root/worker-token
```

`issuance_pending` means OCI signing has been requested, not that enrollment is
complete. Repeat the same command after provider processing until it reports
`enrolled: true`. The checkpoint prevents repeating an uncertain provider request.
Validation and Vault read-back must succeed before activation. Remove temporary
token transfer files after all nodes are enrolled; subsequent renewals read the
existing token from that node's Vault bundle and never rotate it.

Initialize the coordinator ledger using the base guide. Activate the coordinator:

```sh
sudo /opt/migration-installer/bin/migration-runtime activate-ui --reference /root/fixture-reference.json
```

Activate each worker:

```sh
sudo /opt/migration-installer/bin/migration-runtime activate-ui
```

The coordinator command verifies the ledger identity and reference checksum,
creates a separate persistent operator token, and enables the private UI service.
Its token file is `/var/lib/migration-portable/operator-token`; retrieve it through
your approved private administrator channel. It is never printed by the CLI.
Workers enable dispatch polling every 15 seconds. These activation commands also
enable the five-minute renewal timer when managed resources are configured.

Forward coordinator port 8787 through the administrator's authenticated tunnel,
open the forwarded localhost address, and sign in. Check the deployment settings
and worker readiness, create a preview, inspect it, then explicitly start the run.
Use the same synthetic test and independent reference requirements as preview 1.

For a managed installation without the UI, enable
`migration-portable-renew.timer` explicitly after first enrollment. CLI jobs still
require explicit invocation.

## Renewal and failure behavior

- Requests begin when a certificate has at most six hours left. New leaves are
  requested for 24 hours; read-back rejects an invalid subject, purpose, key,
  signature, SAN, authority, lifetime or deployment binding.
- A request-intent checkpoint is persisted **before** calling OCI. Unknown outcomes
  are polled, not blindly resubmitted. After 30 minutes without usable read-back,
  the timer reports attention required. Inspect the provider request before an
  administrator removes that purpose's `*-issuance.json` checkpoint to retry.
- Valid certificates are conditionally published to the assigned Vault secret,
  preserving the deployment token. Existing unknown content cannot be overwritten.
- Activation waits for workers and client commands to be idle. The coordinator
  stops the UI before checking admission/running work; it restarts the database
  only when no runnable active migration or running batch remains, then restarts
  the UI. Paused/drained or expired idle jobs do not prevent renewal. There can be
  a short UI/database interruption during activation; this is not zero-downtime PKI.
- Existing pilot jobs have a one-hour ceiling. New UI admissions require at least
  two hours of certificate lifetime. Hung work or persistent provider failures
  can still require operator attention; the system never fabricates readiness or
  disables certificate validation to proceed.
- Coordinator renewal status appears in the UI, with credential expiry and worker
  heartbeat readiness. Detailed safe status is stored under
  `/var/lib/migration-portable/health/renewal-status.json`. Failed timers also appear
  in systemd. This release has no email/pager integration.
- CA rollover is **not automated**. Renewal stops and reports attention when an
  issuer has two days or less remaining. Plan a reviewed trust transition before
  that boundary. Do not treat this as indefinite unattended production operation.
- OCI certificate versions and Vault secret versions accumulate. Administrative
  retention/cleanup and service-limit monitoring remain required; automatic old
  version deletion is not implemented. Provider quota failures stop renewal safely.

## Qualification and remaining acceptance

Local PostgreSQL tests cover portable preview/start/control, run allowance,
reference drift, worker readiness and cross-deployment rejection. Mocked provider
tests cover successful managed issuance, read-back, repeat polling, unknown
outcomes, wrong issuers, CA expiry and activation deferral. The browser script has
an offline DOM workflow harness, and the HTTP surface has authentication and
same-origin tests. Embedded-browser visual inspection was blocked by the local
browser runtime failing to initialize; no visual browser acceptance is claimed.

The existing demo was not changed. Before handing this off as accepted software,
rehearse the fresh OCI installation, provider IAM, managed certificate profiles,
AWS Roles Anywhere exchange, browser rendering, worker dispatch, reboot, renewal
and a full small migration using the exact packaged source/runtime image. Local
tests do not establish those cloud outcomes. Production inventory, interconnect
qualification, large objects, automatic worker provisioning/scaling and SSO remain
outside this release.
