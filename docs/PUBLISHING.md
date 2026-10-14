# Publish the handoff to GitHub

The initial repository is [Gshroyer34/s3-oci-migration-runner](https://github.com/Gshroyer34/s3-oci-migration-runner), created as private. Recipients need access from the repository owner. The procedure below is retained for publishing another reviewed copy or maintaining release assets. Publish only this curated folder; do not initialize or push the parent workspace containing lab credentials and state.

## Before publishing

- Review `DISTRIBUTION_NOTICE.md`. Choose the intended audience and obtain the appropriate distribution/license decision. A private repository is a sensible initial evaluation channel; this handoff does not grant an open-source license.
- Run `python scripts/verify_handoff.py`. Inspect the manifest and included files.
- Scan the candidate repository and release assets using the organization's secret scanner. The bundled scan detects selected secret patterns and forbidden file types; it is not a general security audit.
- Confirm no generated profiles, host plans, tokens, private keys, runtime-image archives, Terraform state, local cloud configuration, real object manifests or test uploads have been added.
- Review the limitations and current qualification record; do not label this production-ready or fresh-tenancy validated.

## Create the repository

For a new repository only, create an empty repository under the intended owner in GitHub, then, from **this folder**. Existing clones should keep their existing repository and remote:

```sh
git init -b main
git add .
git diff --cached --stat
git status --short
```

Inspect the staged content before committing. Then use your own repository URL:

```sh
git commit -m "Add migration runner engineering preview 2"
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

Use normal GitHub authentication; no access token belongs in the remote URL. Review staged changes before each publication.

## Release assets and reproducibility

The repository contains the compact source TAR and Python wheel in `packages/`; no large runtime image is included. The generated outer ZIP can also be attached as a release asset with its SHA-256 sidecar. Users who clone do not need to download that ZIP separately.

After making changes, use a clean checkout. Rebuild the packages as described in `packages/README.md`, update qualification, then run:

```sh
python scripts/seal_handoff.py --zip /OUTSIDE_REPOSITORY/migration-runner-handoff.zip
python scripts/verify_handoff.py
```

The seal command refuses unrecognized sensitive file types/patterns and writes a manifest of the distribution files, then a ZIP and its checksum. It does not upload, sign or apply cloud resources. It refuses an existing output ZIP/sidecar. Generated cloud configuration must remain outside the repository even when Git ignores it.

Have the recipient compare the ZIP checksum through a trusted channel and run the verifier after extraction. For public releases, add the organization's publisher-signing/provenance process and dependency/license review separately.
