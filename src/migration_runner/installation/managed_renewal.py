"""Unattended leaf renewal using OCI-managed issuers and node-local private keys.

No CA private key or signing passphrase is loaded by this module. An uncertain
issuance is checkpointed before the network call and is not blindly resubmitted.
"""
from datetime import datetime, timedelta, timezone
import json

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import enrollment
from .host_files import InstallError, atomic, encoded, read, sha


class Pending(InstallError):
    pass


def sdk(plan):
    import oci
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    options = dict(signer=signer, timeout=(10,30), retry_strategy=oci.retry.NoneRetryStrategy())
    config = {'region': plan.deployment.destination.region}
    return (oci.certificates_management.CertificatesManagementClient(config, **options),
            oci.certificates.CertificatesClient(config, **options))


def issue(plan, node, directory, tls_ca, roles_ca, management, certificates, now=None):
    import oci
    now = now or datetime.now(timezone.utc)
    if not plan.managed_certificates:
        raise InstallError('Managed certificate resources are not configured.')
    enrollment.trust(plan,tls_ca,roles_ca)
    for pem in (tls_ca,roles_ca):
        ca = x509.load_pem_x509_certificate(pem)
        if ca.not_valid_after_utc <= now + timedelta(days=2):
            raise InstallError('Authority expires within two days. Administrator trust rollover is required.')
    request = enrollment.request_credentials(plan,node.node_id,directory)
    signed = {'format':'migration-signed-certificates/v1','plan_revision':plan.revision,
              'node_id':node.node_id,'certificates':{}}
    pending_any = False
    for entry in plan.managed_certificates:
        if entry.node_id != node.node_id: continue
        result = management.get_certificate(entry.certificate_id)
        meta = result.data
        if meta.lifecycle_state != 'ACTIVE':
            raise Pending('Managed certificate is not active; waiting for provider processing.')
        if (meta.id != entry.certificate_id or meta.compartment_id != plan.deployment.destination.compartment_ocid
                or meta.issuer_certificate_authority_id != entry.authority_id
                or meta.config_type != 'MANAGED_EXTERNALLY_ISSUED_BY_INTERNAL_CA'
                or meta.certificate_profile_type != ('TLS_SERVER' if entry.purpose=='database' else 'TLS_CLIENT')):
            raise InstallError('Managed certificate identity, issuer or profile differs from this plan.')
        response = certificates.get_certificate_bundle(entry.certificate_id,stage='CURRENT',certificate_bundle_type='CERTIFICATE_CONTENT_PUBLIC_ONLY').data
        if response.revocation_status and response.revocation_status.time_revoked:
            raise InstallError('Managed certificate is revoked. Administrator intervention is required.')
        if response.certificate_id != entry.certificate_id or response.version_number != meta.current_version.version_number:
            raise Pending('Certificate versions are changing; retry on the next timer interval.')
        pair = {'certificate':response.certificate_pem,'private_key':read(directory/(entry.purpose+'.key')).decode()}
        usable = False
        try:
            cert = x509.load_pem_x509_certificate(pair['certificate'].encode())
            key = serialization.load_pem_private_key(pair['private_key'].encode(),password=None)
            cert.verify_directly_issued_by(x509.load_pem_x509_certificate(roles_ca if entry.purpose=='roles' else tls_ca))
            usable = (cert.subject == enrollment.subject(plan,node.node_id,entry.purpose)
                and enrollment.public_bytes(cert)==enrollment.public_bytes(key)
                and cert.not_valid_before_utc <= now and cert.not_valid_after_utc > now+timedelta(hours=6))
        except (ValueError,TypeError):
            pass
        checkpoint = directory/(entry.purpose+'-issuance.json')
        if usable:
            signed['certificates'][entry.purpose] = pair['certificate']
            checkpoint.unlink(missing_ok=True)
            continue
        if checkpoint.exists():
            previous = json.loads(read(checkpoint))
            if previous['certificate_id'] != entry.certificate_id or previous['plan_revision'] != plan.revision:
                raise InstallError('Pending issuance belongs to a different installation.')
            if now-datetime.fromisoformat(previous['requested_at']) > timedelta(minutes=30):
                raise InstallError('Issuance has not completed within 30 minutes. Inspect the provider request before retrying it.')
            pending_any = True
            continue
        if not result.headers.get('etag'):
            raise InstallError('Certificate provider did not return a concurrency token.')
        # Record intent before the request. Timeouts or process death cannot cause
        # a timer to consume unbounded certificate versions through blind retries.
        atomic(checkpoint,encoded({'certificate_id':entry.certificate_id,'plan_revision':plan.revision,
            'requested_at':now.isoformat(),'previous_version':response.version_number}))
        management.update_certificate(entry.certificate_id,
            oci.certificates_management.models.UpdateCertificateDetails(certificate_config=
                oci.certificates_management.models.UpdateCertificateManagedExternallyIssuedByInternalCaConfigDetails(
                    csr_pem=request['requests'][entry.purpose],stage='CURRENT',validity=
                    oci.certificates_management.models.Validity(time_of_validity_not_before=now-timedelta(minutes=5),
                                                              time_of_validity_not_after=now+timedelta(hours=24)))),
            if_match=result.headers['etag'])
        pending_any = True
    if pending_any:
        raise Pending('Certificate issuance requested; waiting for provider read-back.')
    return signed


def renew(plan, node, directory, tls_ca, roles_ca, reader, writer, management, certificates, token=None):
    current = enrollment.decode_secret(reader.get_secret_bundle(node.secret_id,stage='CURRENT'))
    initial = current == {'format':'migration-enrollment-pending/v1','plan_revision':plan.revision,'node_id':node.node_id}
    if not initial:
        if (current.get('format')!='migration-node-credentials/v1' or current.get('plan_revision')!=plan.revision
                or current.get('node_id')!=node.node_id or sha(str(current.get('worker_token','')).encode())!=plan.worker_token_sha256):
            raise InstallError('Vault credentials do not belong to this installation.')
        token = current['worker_token']
    if not token or sha(token.encode()) != plan.worker_token_sha256:
        raise InstallError('Initial managed enrollment requires the deployment token file.')
    signed = issue(plan,node,directory,tls_ca,roles_ca,management,certificates)
    bundle = enrollment.assemble(plan,node.node_id,signed,directory,token)
    # Full certificate/key/CN/EKU/SAN/lifetime verification occurs before Vault is
    # modified. Enrollment preserves the token and conditionally replaces CURRENT.
    result = enrollment.enroll(plan,node.node_id,bundle,reader,writer,tls_ca,roles_ca,
                              replace_digest=None if initial else sha(encoded(current)))
    return {**result,'changed':current!=bundle}
