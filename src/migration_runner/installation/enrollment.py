"""Deployment-owned certificate requests and conditional, exact-secret enrollment.

Authority keys are used only by the offline signing functions. Host operations use
node-local leaf keys and OCI instance identity; provider errors are never printed.
"""
import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .host_files import InstallError, atomic, encoded, read, sha


def fingerprint(pem):
    return x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex()


def trust(plan, tls_ca, roles_ca):
    if fingerprint(tls_ca) != plan.tls_ca_sha256 or fingerprint(roles_ca) != plan.roles_ca_sha256:
        raise InstallError('Certificate authority fingerprint differs from the reviewed plan.')
    for pem in (tls_ca, roles_ca):
        cert = x509.load_pem_x509_certificate(pem)
        cert.verify_directly_issued_by(cert)
        if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise InstallError('Trust anchor is not an authority.')
        if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise InstallError('Authority cannot sign certificates.')


def key_bytes(key, password=None):
    protection = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, protection)


def public_bytes(key):
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def usage(ca=False):
    return x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
        encipher_only=False, decipher_only=False)


def create_authorities(directory, name, password):
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,23}', name) or len(password) < 12:
        raise InstallError('Use a valid deployment name and an authority passphrase of at least 12 characters.')
    # Output must be new: never replace an existing authority or reuse a key silently.
    directory.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    for kind in ('tls', 'roles'):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'migration-{name}-{kind}-ca')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(usage(True), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
        atomic(directory / (kind + '-ca.key'), key_bytes(key, password))
        atomic(directory / (kind + '-ca.crt'), cert.public_bytes(serialization.Encoding.PEM))
    return {kind: fingerprint(read(directory / (kind + '-ca.crt'))) for kind in ('tls', 'roles')}


def subject(plan, node_id, purpose):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, plan.common_name(node_id, purpose))])


def request_credentials(plan, node_id, directory):
    """Idempotently retain node-local keys until this enrollment is completed."""
    request = {'format': 'migration-certificate-request/v1', 'plan_revision': plan.revision,
               'node_id': node_id, 'requests': {}}
    for purpose in plan.purposes(node_id):
        path = directory / (purpose + '.key')
        if path.exists():
            key = serialization.load_pem_private_key(read(path), password=None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            atomic(path, key_bytes(key))
        builder = x509.CertificateSigningRequestBuilder().subject_name(subject(plan, node_id, purpose))
        if purpose == 'database':
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(plan.node('coordinator').private_ip))]), critical=False)
        csr = builder.sign(key, hashes.SHA256())
        request['requests'][purpose] = csr.public_bytes(serialization.Encoding.PEM).decode()
    return request


def sign_requests(plan, request, authorities, password):
    if set(request) != {'format', 'plan_revision', 'node_id', 'requests'} or request['format'] != 'migration-certificate-request/v1' or request['plan_revision'] != plan.revision:
        raise InstallError('Certificate request is not bound to this reviewed installation.')
    node_id = request['node_id']
    if set(request['requests']) != set(plan.purposes(node_id)):
        raise InstallError('Certificate request purposes differ from this node role.')
    cas = {kind: read(authorities / (kind + '-ca.crt')) for kind in ('tls', 'roles')}
    trust(plan, cas['tls'], cas['roles'])
    result = {'format': 'migration-signed-certificates/v1', 'plan_revision': plan.revision,
              'node_id': node_id, 'certificates': {}}
    now = datetime.now(timezone.utc)
    for purpose, pem in request['requests'].items():
        csr = x509.load_pem_x509_csr(pem.encode())
        if not csr.is_signature_valid or csr.subject != subject(plan, node_id, purpose):
            raise InstallError('CSR signature or subject rejected.')
        if not isinstance(csr.public_key(), ec.EllipticCurvePublicKey) or not isinstance(csr.public_key().curve, ec.SECP256R1):
            raise InstallError('Only the generated P-256 enrollment keys are accepted.')
        expected = [x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(plan.node('coordinator').private_ip))])] if purpose == 'database' else []
        if [extension.value for extension in csr.extensions] != expected:
            raise InstallError('CSR extensions differ from the reviewed role and address.')
        kind = 'roles' if purpose == 'roles' else 'tls'
        ca = x509.load_pem_x509_certificate(cas[kind])
        key = serialization.load_pem_private_key(read(authorities / (kind + '-ca.key')), password=password)
        if public_bytes(key) != public_bytes(ca):
            raise InstallError('Authority key does not match its pinned certificate.')
        expiry = min(now + timedelta(hours=24), ca.not_valid_after_utc)
        if ca.not_valid_before_utc > now or expiry <= now + timedelta(hours=2):
            raise InstallError('Authority expires too soon; review authority rollover first.')
        builder = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expiry)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(usage(), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if purpose == 'database' else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
        for extension in csr.extensions:
            builder = builder.add_extension(extension.value, critical=extension.critical)
        result['certificates'][purpose] = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()
    return result


def validate_bundle(plan, node_id, bundle, tls_ca, roles_ca, now=None):
    now = now or datetime.now(timezone.utc)
    trust(plan, tls_ca, roles_ca)
    fields = {'format', 'plan_revision', 'node_id', 'worker_token', 'credentials'}
    if set(bundle) != fields or bundle['format'] != 'migration-node-credentials/v1' or bundle['plan_revision'] != plan.revision or bundle['node_id'] != node_id:
        raise InstallError('Credential bundle does not belong to this deployment and node.')
    token = bundle['worker_token']
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', token) or sha(token.encode()) != plan.worker_token_sha256:
        raise InstallError('Credential token does not match this deployment.')
    if set(bundle['credentials']) != set(plan.purposes(node_id)):
        raise InstallError('Unexpected credentials for this node role.')
    expiries = []
    for purpose, pair in bundle['credentials'].items():
        if set(pair) != {'certificate', 'private_key'}:
            raise InstallError('Malformed credential pair.')
        cert = x509.load_pem_x509_certificate(pair['certificate'].encode())
        key = serialization.load_pem_private_key(pair['private_key'].encode(), password=None)
        ca = x509.load_pem_x509_certificate(roles_ca if purpose == 'roles' else tls_ca)
        cert.verify_directly_issued_by(ca)
        if cert.subject != subject(plan, node_id, purpose) or public_bytes(cert) != public_bytes(key):
            raise InstallError('Certificate subject or node-local key differs.')
        if not ca.not_valid_before_utc <= now < ca.not_valid_after_utc or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc - timedelta(hours=1):
            raise InstallError('Certificate is expired, not valid yet, or has less than one hour remaining.')
        if cert.not_valid_after_utc > ca.not_valid_after_utc or cert.not_valid_after_utc - cert.not_valid_before_utc > timedelta(hours=25):
            raise InstallError('Certificate lifetime exceeds the enrollment limit.')
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca or not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
            raise InstallError('Invalid leaf certificate constraints.')
        expected_eku = [ExtendedKeyUsageOID.SERVER_AUTH if purpose == 'database' else ExtendedKeyUsageOID.CLIENT_AUTH]
        if list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != expected_eku:
            raise InstallError('Certificate purpose differs from its node role.')
        if purpose == 'database':
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if list(san) != [x509.IPAddress(ipaddress.ip_address(plan.node('coordinator').private_ip))]:
                raise InstallError('Database certificate address differs from the coordinator.')
        expiries.append(cert.not_valid_after_utc)
    return min(expiries)


def assemble(plan, node_id, signed, directory, token):
    if set(signed) != {'format', 'plan_revision', 'node_id', 'certificates'} or signed['format'] != 'migration-signed-certificates/v1' or signed['node_id'] != node_id or signed['plan_revision'] != plan.revision or set(signed['certificates']) != set(plan.purposes(node_id)):
        raise InstallError('Signed certificate response does not match this node request.')
    return {'format': 'migration-node-credentials/v1', 'plan_revision': plan.revision, 'node_id': node_id,
            'worker_token': token, 'credentials': {purpose: {'certificate': signed['certificates'][purpose],
            'private_key': read(directory / (purpose + '.key')).decode()} for purpose in plan.purposes(node_id)}}


def decode_secret(response):
    content = response.data.secret_bundle_content
    if content.content_type != 'BASE64' or len(content.content) > 48000:
        raise InstallError('Vault credential content exceeds the supported envelope.')
    return json.loads(base64.b64decode(content.content, validate=True))


def enroll(plan, node_id, bundle, reader, writer, tls_ca, roles_ca, replace_digest=None):
    """Read CURRENT, compare-and-swap with ETag, then verify CURRENT read-back.

    An unknown existing secret is never overwritten. A same-plan renewal requires
    the operator's previous content digest, so a retry cannot silently rotate keys.
    """
    import oci
    validate_bundle(plan, node_id, bundle, tls_ca, roles_ca)
    secret_id = plan.node(node_id).secret_id
    metadata = writer.get_secret(secret_id)
    current_response = reader.get_secret_bundle(secret_id, stage='CURRENT')
    current = decode_secret(current_response)
    payload = encoded(bundle)
    if len(payload) > 24000:
        raise InstallError('Credential payload is too large for the supported Vault enrollment.')
    if current != bundle:
        placeholder = {'format': 'migration-enrollment-pending/v1', 'plan_revision': plan.revision, 'node_id': node_id}
        if current != placeholder:
            if (not replace_digest or sha(encoded(current)) != replace_digest
                    or current.get('format') != 'migration-node-credentials/v1'
                    or current.get('plan_revision') != plan.revision or current.get('node_id') != node_id
                    or sha(str(current.get('worker_token', '')).encode()) != plan.worker_token_sha256):
                raise InstallError('Existing Vault secret differs; renewal requires its reviewed previous digest.')
        if metadata.data.current_version_number != current_response.data.version_number or not metadata.headers.get('etag'):
            raise InstallError('Vault changed during enrollment; inspect and retry.')
        details = oci.vault.models.UpdateSecretDetails(secret_content=oci.vault.models.Base64SecretContentDetails(
            content=base64.b64encode(payload).decode(), stage='CURRENT'))
        writer.update_secret(secret_id, details, if_match=metadata.headers['etag'])
    if decode_secret(reader.get_secret_bundle(secret_id, stage='CURRENT')) != bundle:
        raise InstallError('Vault read-back differs; no host activation was performed.')
    return {'enrolled': True, 'node_id': node_id, 'credential_digest': sha(payload),
            'expires_at': validate_bundle(plan, node_id, bundle, tls_ca, roles_ca).isoformat()}


def new_token():
    return secrets.token_urlsafe(48)
