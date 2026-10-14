# Configuration examples

Copy these into a private workspace outside the repository and replace every `REPLACE_...` value before validating. They deliberately fail validation until completed. Never commit the completed copies. Use actual stack outputs; inventing an OCID or ARN does not create a resource.

- `installation-profile.example.json`: setup assistant/CLI input. This quick start uses `fixtures/` because the supplied fixture generator emits that prefix. Initial and maximum workers are both 1 for the smallest trial; increase deliberately before deployment if required.
- `reader-identity.example.json`: values from AWS CloudFormation `ReaderRoleArn`, `TrustAnchorArn`, `ReaderProfileArn`.
- `nodes.example.json`: OCI foundation `nodes` output plus each node's own Vault secret OCID.
- `features-ui.example.json`: UI with manual signing. Hash the exact local `reference.json` file and set its hex SHA-256.
- `features-managed-one-worker.example.json`: UI plus all five required managed certificates for one coordinator/one worker. For each additional worker add distinct `tls` and `roles` resource entries. Two distinct CA IDs are required: one for tls/database and one for roles.

The UI lifetime admission allowance is set before installing. Optional fields are omitted deliberately. Do not add secret values to satisfy a validation error.
