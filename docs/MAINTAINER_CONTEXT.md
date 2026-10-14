# Maintainer context and development handoff

This folder is a curated source snapshot for an independent OCI evaluation. It is not a copy of the original operator workstation. No previous tenancy access, tokens, state, build caches or source data are needed or included. Read the root README and limitations before changing behavior.

## Project intent and release state

The long-term goal is an S3-to-OCI migration tool with immutable scope, verified copies, tag/property preservation, auditable manifests and bandwidth-aware capacity control. The immediate release is a bounded synthetic pilot. Full production migration and automatic scaling remain future work.

Core package version is 0.5.0; portable installer preview 2 uses ledger schema `0007_portable_workers`. Both coordinator and workers must use the same reviewed source/dependency contract. This snapshot includes selected regression tests, not every historical lab/benchmark automation script.

## Code map

| Path | Responsibility |
| --- | --- |
| `src/migration_runner/installation/profile.py`, `package.py`, `aws.py` | Validated installation input, OCI foundation generation, AWS reader template |
| `installation/deployment.py` | Immutable deployment scope and installed-profile authorization |
| `installation/host_plan.py`, `host_release.py`, `host_runtime.py` | Node plans, host release integrity, Linux services/ledger bootstrap |
| `installation/enrollment.py`, `managed_renewal.py` | Local key/CSR handling, certificate checks, Vault enrollment and managed issuer adapter |
| `installation/portable_host.py`, `host_cli.py` | UI/dispatch/renewal activation and operation locking |
| `portable_ui.py`, `portable_agent.py`, `cloud_api.py` | Deployment-scoped operator API and fixed-node dispatch |
| `cloud_static/portable.html`, `portable.js` | Portable browser interface |
| `worker/cloud.py`, `tags.py`, `properties.py`, `queue.py` | Cloud transfer, compatibility mapping, verification and ledger completion |
| `autoscaling/` | Pure recommendation evaluator, telemetry contracts and read-only monitoring |
| `alembic/versions/`, `deploy/runtime/database-roles.sql` | Schema evolution and scoped database privileges |
| `src/migration_runner/installation/templates/` | Resource Manager templates and generated-package documentation |

Legacy lab modules and their fixed example identities remain for compatibility tests and existing imports. They are not the installation route. Do not invoke lab scripts as a shortcut to configuring a portable tenancy. Root `scripts/lab_pki.py`, `assemble_runtime_secret.py` and `enroll_cloud_worker.py` are present because the existing cloud image recipe includes them; use `migration-runtime` for this handoff.

## Invariants to preserve

- Jobs cannot supply or replace their own deployment authority. Workers check independently installed, root-owned profiles and revisions.
- Keep source freeze, exclusive run prefixes, content/tag/property baselines, scoped admission and bounded retries.
- Never change verification or limits silently to make a pilot pass.
- Do not treat metadata as equivalent IAM, ownership, lifecycle or retention policy.
- Keep secrets out of arguments, logs, Terraform state, manifests and source packages.
- Preserve private keys/unknown database state on failure; do not implement destructive retries.
- Managed issuance checkpoints must survive uncertain provider outcomes. Never blindly resubmit after a timeout.
- Distinguish desired capacity from ready capacity, and wire traffic from useful verified bytes.

## Local development and tests

Use the Python 3.12 environment from the README, then:

```sh
python -m pip install -r requirements-dev.lock
python -m pytest -q tests/test_installation.py tests/test_deployment_profile.py tests/test_host_installer.py tests/test_portable_ui_renewal.py tests/test_fixture_generator.py
node tests/portable_ui_model.test.js
```

Node is optional unless running the browser-model harness. It uses a fake DOM and does not establish rendered-browser acceptance. Windows skips Linux-root tests; database tests skip unless explicitly enabled. Inspect skip reasons rather than reporting skips as passes. OCI provider calls in installer tests use substitutes; they do not require live credentials.

For disposable database tests only, create a **separate local PostgreSQL 17.11** database named `migration`, bound to loopback on port 55432, with a dedicated test user/password. Set `MIGRATION_TEST_POSTGRES=1`, `MIGRATION_DB_HOST=127.0.0.1`, `MIGRATION_DB_PORT=55432`, `MIGRATION_DB_NAME=migration`, `MIGRATION_DB_USER` and `MIGRATION_DB_PASSWORD` in the test process. The fixture creates and drops `test_<uuid>` schemas. Never point it at a production or demo ledger. This handoff does not ship a preconfigured database password.

Backend protocol tests need Rclone 1.75.1; the cloud container recipe supplies the pinned binary. These local fake-service tests still are not cloud acceptance. Root file/service tests belong in a disposable Linux environment, never on an installed runtime node.

```sh
python -m migration_runner.autoscaling --policy fixtures/autoscaling/10gbps-policy.json --input fixtures/autoscaling/10gbps-shadow.jsonl --output /PRIVATE/shadow-result.jsonl
```

This replay uses synthetic observations and rates. All decisions remain advisory; `automatic` mode is rejected. The runtime implementation still lacks a validated shared-path meter and enforced bandwidth grants.

## Suggested development order

1. Rehearse fresh install, actual IAM, real browser, portable dispatch, reboot and managed renewal. Fix observed gaps and retain a reproducible acceptance record.
2. Automate remaining issuer/destination IAM provisioning, provide a reviewed upgrade/backup procedure and simplify installation without weakening isolation.
3. Implement shared bandwidth enforcement and bounded concurrency on a fixed fleet; persist policy ownership/decision history.
4. Add application-aware worker drain, idempotent provisioning, dynamic credential enrollment and auto-scaling only after rate control is measured independently.
5. Build production inventories, larger-object/version support, Standard-first/Glacier Instant Retrieval admission, lifecycle discovery and scalable manifests. Keep Deep Archive excluded unless scope is explicitly changed.
6. Add SSO/roles, alerting, CA rollover, retention and operational availability appropriate to production.

## Updating the handoff

Build from this tree, not an operator's `.secrets`, `tmp`, `infra` state or cached images. Re-run tests, regenerate the source installer and wheel, update `QUALIFICATION.json`, then seal/package with `scripts/seal_handoff.py`. Checksum changes require a new reviewed distribution checksum. Do not edit the final ZIP after sealing.

Publisher identity, release signatures and licensing are decisions for the repository owner. Do not assume a checksum alone authenticates the publisher.
