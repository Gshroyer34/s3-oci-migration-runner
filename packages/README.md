# Included packages

- `migration-installer-handoff-preview-2.tar`: compact source installer, database migrations, build recipes, templates and technical guides. Extract on a trusted Linux build machine or verified fresh OCI node. This is a newly packaged handoff source artifact; its checksum differs from the earlier internal preview archive.
- `s3_oci_migration_runner-0.5.0-py3-none-any.whl`: application Python wheel. Requires Python 3.12 and separately installed locked dependencies. It is not an OCI machine image or complete runtime container.
- `SHA256SUMS.txt`: package checksums; the root handoff manifest also covers each package.

No runtime-image TAR, PostgreSQL image, offline wheelhouse, private keys or environment-specific host release is included. Build the runtime image using [deployment stage 5](../docs/DEPLOYMENT.md). Build a separate host release only after configuring your tenancy and identities.

## Rebuild after source changes

Use a clean tree and a Python 3.12 environment with the build locks installed. Existing output files are not overwritten by the source builder; choose a new versioned filename when changing a published release.

```sh
python scripts/build_portable_installer.py --output /PRIVATE/migration-installer-next.tar
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /PRIVATE/wheels .
```

Review the outputs and replace the intended package files in a new release working copy, updating names/documentation as needed. `scripts/seal_handoff.py` refreshes package checksums and the handoff manifest. It does not rebuild the wheel/container for you.
