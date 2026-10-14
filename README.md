# S3 to OCI Migration Runner

An engineering preview for running a small, verified S3-to-OCI Object Storage migration in your own OCI tenancy. Includes a private browser UI, coordinator, workers, installation tools, AWS reader templates, and optional unattended leaf-certificate renewal. 

**Release: GitHub handoff preview 2 · October 14, 2026 · Python package 0.5.0.**

This is a hands-on evaluation release for cloud administrators. It is not a one-click installer or a production migration service. Fresh-tenancy installation, managed certificate issuance, reboot and portable UI cloud acceptance remain to be completed. Use synthetic test data first.

## Supported trial

| Setting | Current boundary |
| --- | --- |
| Hosts | Fresh Oracle Linux 9, x86_64; one coordinator and 1–4 fixed workers |
| Source | One approved S3 bucket/prefix; immutable synthetic objects; Standard tier; SSE-S3 |
| Destination | Existing OCI bucket; a fresh, exclusively owned run prefix |
| Dataset | Up to 100 objects, 10,000,000,000 bytes total, 100,000,000 bytes per object |
| Runtime | At most one hour per admitted run; full-read SHA-256 verification |
| Network | Approved internet route; private-route foundation templates exist but portable execution rejects private mode |
| UI | Private loopback listener through SSH/Bastion; separate operator token; 1–10 lifetime admissions chosen at installation |
| Metadata | Reversible S3 tags, supported native headers and compatible user metadata, plus exported evidence |
| Scaling | Fixed fleet; recommendation engine is included, automatic scaling is not enabled |

**Do not use this release for the 750 TB migration, Glacier Instant Retrieval, Deep Archive, 10 GB objects, SSE-KMS payloads or live mutable data.** Larger OCI-only benchmark results describe a separate harness and do not expand these limits.

## Start here

1. Read [prerequisites and information to gather](docs/PREREQUISITES.md).
2. Follow [deployment from clone to first login](docs/DEPLOYMENT.md), including the manual infrastructure/IAM steps.
3. Run the [small pilot and acceptance checklist](docs/FIRST_PILOT.md).
4. Keep [operations and troubleshooting](docs/OPERATIONS.md) available during the trial.

For a local preview without provisioning anything, use Python 3.12 on Windows or Linux:

```sh
git clone https://github.com/Gshroyer34/s3-oci-migration-runner.git migration-runner
cd migration-runner
python scripts/verify_handoff.py
python -m venv .venv
```

Activate the environment with `source .venv/bin/activate` on Linux or `.venv\Scripts\Activate.ps1` in PowerShell. If local policy blocks activation, invoke `.venv\Scripts\python.exe` directly; administrator access is not needed to run the setup assistant.

```sh
python -m pip install -r requirements-cloud.lock -r requirements-build.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m migration_runner.installation serve
```

Open `http://127.0.0.1:8793/static/install.html`. This is the **setup assistant**, not the running migration dashboard. It validates configuration and generates templates; it does not install cloud resources. Stop it with Ctrl+C. The GitHub repository is private; recipients need repository access before cloning.

## Where to find things

| Resource | Purpose |
| --- | --- |
| [Deployment guide](docs/DEPLOYMENT.md) | Ordered installation, build, IAM, enrollment and login instructions |
| [Architecture and security](docs/ARCHITECTURE_AND_SECURITY.md) | Data flow, trust boundaries, required permissions and credentials |
| [IAM and network checklist](docs/IAM_AND_NETWORK.md) | Exact-node policies, destination policy review template and required network flows |
| [Shape and performance guidance](docs/SHAPES_AND_PERFORMANCE.md) | Measured Optimized3/AMD/Ampere comparisons and their limits |
| [FAQ](docs/FAQ.md) | Plain-language answers for operators and application owners |
| [Compatibility guide](docs/COMPATIBILITY.md) | Tags, metadata, owner, ETag, lifecycle, encryption and verification |
| [Known limitations](docs/KNOWN_LIMITATIONS.md) | Implemented, tested, pending and unsupported behavior |
| [Maintainer context](docs/MAINTAINER_CONTEXT.md) | Code map, test commands, design constraints and development priorities |
| [Publication checklist](docs/PUBLISHING.md) | How the owner can publish this folder and release assets |
| [Examples](examples/README.md) | Nonsecret input templates; replace placeholders before validation |
| [Packages](packages/README.md) | Source installer and Python wheel; runtime image must be built separately |
| `src/`, `alembic/`, `deploy/` | Application, database migrations and bootstrap/build resources |
| `tests/`, `fixtures/autoscaling/` | Selected offline/integration regression tests and synthetic controller traces |
| `HANDOFF_MANIFEST.json` | File inventory and SHA-256 checksums, verified by the included script |

## Important ownership details

Use deployment-owned AWS/OCI accounts, keys, certificates and tokens. No working cloud credentials, Terraform state, node plans or runtime secrets are supplied. Historical lab identifiers remain in legacy compatibility code and regression fixtures; the portable path requires your independently installed deployment profile and does not use those identities.

Keep generated configuration and secrets outside the checkout, in an access-controlled deployment workspace. The supplied `.gitignore` is a backstop, not a secret store. Protect manifests too: they can contain source names, tags and owner information.

See [distribution notice](DISTRIBUTION_NOTICE.md) before public publication. No open-source license is assigned by this handoff. Existing dependency licenses remain applicable.
