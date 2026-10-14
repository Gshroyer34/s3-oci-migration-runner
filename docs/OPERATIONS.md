# Operations and troubleshooting

Keep the release checksum, image ID, host-plan revision, stack outputs, issuer expiry dates and job IDs in a private deployment record. Never reset the ledger to resolve a login or certificate problem.

## Routine checks on a node

```sh
sudo /opt/migration-installer/bin/migration-runtime status
sudo systemctl status migration-portable-db.service
sudo systemctl status migration-portable-ui.service
sudo systemctl list-timers 'migration-portable-*'
```

The database/UI services belong on the coordinator; workers have dispatch/worker units instead. A unit absent from that node is not automatically a failure. Check the installed units with `systemctl list-unit-files 'migration-portable-*'`. Read logs with `journalctl -u UNIT --since '30 minutes ago'`; review them locally before sharing.

The coordinator renewal health file is `/var/lib/migration-portable/health/renewal-status.json`. Service status is not proof of cloud access or verified migration completion.

## Common problems

| Symptom | Check and next step |
| --- | --- |
| Cannot open local port 8787 | Renew/check Bastion session, SSH authentication and local forward to coordinator loopback. Confirm UI service and local port availability. Do not expose the UI publicly. |
| UI says unauthorized | Use the coordinator operator token, not the worker token. Reload/log in; browser memory loses the token on refresh. |
| Setup AWS tests time out | Check VPN/proxy, DNS and permitted AWS endpoints from the workstation. Retest runtime nodes separately. |
| Configuration validation fails | Replace all example placeholders; check account/region, prefix trailing slash, image architecture, administrator CIDR and public-key encoding. |
| Stack plan refuses foundation scope | Read the generated foundation guide and deliberately set its acknowledgement. It still will not install the app. |
| Shape capacity unavailable | Choose another supported shape/AD with an appropriate image; regenerate consistent profiles before installation. |
| Node install rejects identity or checksum | Verify exact node ID, instance OCID/IP, plan, source release checksum and image ID. Do not bypass checks. |
| Vault access denied | Exact-instance group membership, compartment, own-secret policies, placeholder plan revision and IAM propagation. Managed mode retains update rights. |
| Certificate issuance pending | Wait for OCI processing and rerun the same enrollment/renew command. Do not delete intent checkpoints or repeatedly create resources. |
| Renewal needs attention after 30 minutes | Inspect provider request/version and the local issuance checkpoint. Resolve uncertain outcomes before an administrator resets a checkpoint. |
| AWS Roles Anywhere exchange fails | Correct roles CA trust anchor, expected subject, assigned role/profile, certificate validity, endpoint access and clock. See the AWS stack outputs. |
| Worker never becomes ready | Enrollment, restored credentials, dispatch timer, TCP 5432 both NSG/firewall, correct ledger TLS identity and deployment revision. |
| Inventory differs from reference | Wrong prefix, extra file such as `reference.json`, missing upload, changed size/content or stale manifest. Keep source frozen and repair the fixture setup. |
| Object blocked | Read the specific tier/encryption/metadata/retention/reference error; do not silently drop metadata or weaken verification. |
| No remaining UI runs | Lifetime plan allowance exhausted. Export evidence; arrange another reviewed deployment or future upgrade path. Do not edit ledger counters. |
| Renewal activation deferred | Active work/client containers can prevent safe credential switching. Drain intentionally, preserve work, then allow activation. |
| Installer says already installed | Same-release `install` may be retried. Different plans/releases and OS bootstrap are not in-place upgrade paths. |

## Credentials and renewals

Manual mode: use the base guide to request/sign/enroll replacements with the previous credential digest, restore temporary own-secret update permission only for enrollment, and drain/stop affected work before restoring mounted credentials. Expired leaves require new valid leaves; restarting the UI does not renew them.

Managed mode: the timer runs every five minutes and requests leaves when at most six hours remain. Certificate and secret versions accumulate. Assign an administrator to retention and quota monitoring. CA rollover is not automated; renewal stops when an issuer has two days or less remaining. Plan trust replacement before that point.

The renewal activation path can briefly interrupt the UI/database while idle. It is not zero-downtime certificate rotation. Persistent active or hung work, provider failures or expired trust can require operator intervention.

## Stop or recover work

Use dashboard pause/drain/cancel controls and wait for workers to reach the appropriate state before maintenance. Pause is not an instant network kill switch. Preserve the source freeze and exclusive destination ownership across retries. Do not manually mark objects verified.

Back up the ledger with an administrator-reviewed PostgreSQL procedure and test recovery in an isolated environment. This release supplies no automated backup/restore command. Preserve the installation state and encryption/credential material required to interpret or resume that ledger. Never copy a live database directory casually and call it a consistent backup.

## Cleanup after an evaluation

1. Stop admitting new work, drain active workers and export the evidence you need.
2. Record the exact resources and test prefixes owned by this deployment. Inspect incomplete multipart uploads after interrupted work.
3. Have the data owner approve removal of synthetic destination/source objects; do not delete a whole pre-existing bucket or unrelated prefixes.
4. Review the OCI stack destroy plan against recorded ownership. Preserve needed ledger/boot-volume evidence first. Stopping compute does not remove storage charges.
5. Review removal of manually created secrets, certificates, issuers/keys and policies separately; they are not all owned by the foundation stack.
6. Remove the AWS reader stack when no longer needed; it owns authorization, not the source bucket.
7. Recheck remaining resources and costs. The package installs no cleanup scheduler or hard billing cap.
