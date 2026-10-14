# First pilot and acceptance checklist

Use a new synthetic source bucket/prefix and dedicated destination bucket. Start with three 1 MB objects and one worker. This proves correctness and access; it is too small to establish throughput.

## Prepare independent fixtures

From the repository root, with Python available:

```sh
python scripts/generate_pilot_fixture.py --output /PRIVATE/pilot-fixture --objects 3 --object-bytes 1000000
```

The generator writes three binary files and `reference.json` with independent SHA-256 hashes. It never uploads or overwrites an existing directory. Keep a failed partial directory for inspection and choose another output path. The generator always uses source keys under `fixtures/`; configure that exact prefix for this quick start.

Using an authorized fixture-uploader identity, upload only the binary files into an initially empty `fixtures/` prefix:

```sh
aws s3 cp /PRIVATE/pilot-fixture/ s3://YOUR_TEST_BUCKET/fixtures/ --recursive --exclude '*' --include '*.bin' --sse AES256 --storage-class STANDARD --region YOUR_AWS_REGION --profile YOUR_FIXTURE_UPLOADER_PROFILE
```

This command writes the test objects. It is an explicit administrator action, not a permission granted to the migration reader. Confirm the bucket/account before running it. Do not upload `reference.json`; any extra object in the prefix will fail the inventory/reference comparison. Use no retention lock, unsupported metadata or lifecycle transitions on this fixture bucket.

Check object count, byte sizes, Standard tier and SSE-S3 (`AES256`) in S3. Freeze content, metadata and tags. Copy `reference.json` privately to the coordinator as `/root/reference.json`, and use its exact file SHA-256 in `features.json` before creating the host plan. The manifest is not generated from destination data.

## Before starting

- [ ] Source and target account/bucket/prefix values match the approved deployment.
- [ ] The run destination prefix is empty and exclusively owned by this trial.
- [ ] All intended nodes report ready; certificates have sufficient remaining lifetime.
- [ ] Each node can read its own Vault bundle and is denied another node's secret; managed certificate permissions are similarly isolated.
- [ ] The operator understands the lifetime UI run allowance and has reserved a run for restart/renewal acceptance if needed.
- [ ] No production source objects are in scope. No cloud-wide credentials were put in the browser or host plan.

## Run from the dashboard

1. Sign in through the SSH tunnel with the separate operator token.
2. Review the installed scope and limits. Create a preview.
3. Confirm the preview contains exactly three objects and 3,000,000 bytes. Resolve every blocked item before starting.
4. Select one worker and explicitly start. Observe worker readiness, batch progress and any failure reason.
5. Require successful content, tags and supported-property verification for every object. An empty original tag map should still be explicitly verified, not absent.
6. Download the manifest; confirm all three source keys and sizes, current-attempt results and matching part checksums. Owner/ACL fields can report `not_captured` if access was denied; review that separately.
7. Independently inspect the OCI objects and metadata, including `opc-meta-migration-s3-tags-v1`.

Use a fresh destination prefix for every later run. The portable UI creates scoped run prefixes. Retrying a failed object must not bypass its original payload/tag/property evidence.

## Recipient acceptance record

Record pass/fail and evidence for these checks in your private deployment record:

| Check | Expected outcome |
| --- | --- |
| Wrong or absent operator token | UI API access rejected |
| Workload source reads | Correct account/bucket/prefix; no source writes granted |
| Destination operations | Approved bucket listing/HEAD/upload work; unsupported scope is rejected by runtime |
| Three-object migration | All objects content/tag/property verified; manifest downloads |
| Cross-deployment job or changed profile | Refused, not silently adopted |
| Manual pause/drain/resume | Work follows controls; active evidence is retained |
| Host reboot while idle | Ledger preserved; credentials restored from Vault; expected services return |
| Managed renewal, if selected | Actual OCI-issued leaves enrolled/read back; activation waits for safe idle; UI recovers |
| Expired/invalid credentials or unavailable Vault | Safe failure; no TLS bypass or false ready state |
| Browser behavior | Login, preview, progress, controls and download usable in the recipient browser |
| Cleanup | Resources, fixture objects and incomplete multipart uploads inventoried and handled deliberately |

Do not alter clocks, revoke live keys or kill active workers to test failure paths on a valuable deployment. Perform destructive fault tests only in a disposable evaluation environment. Managed leaves normally renew with six hours remaining; observe a real cycle or design a separate disposable shortened-lifetime test without weakening validation.

## Further useful tests

After the smoke test passes, use another explicitly scoped synthetic trial to exercise nonempty tags, case-distinct keys, Unicode tag values, native content headers and permitted user metadata. Keep metadata within the application budget. Test multipart behavior with an object within the 100 MB ceiling and confirm the backend actually used multipart. A successful tiny-object run does not prove these paths.

Do not increase hardcoded limits to turn this pilot into a production migration. Track any required larger object, tier, network or inventory work in [known limitations](KNOWN_LIMITATIONS.md).
