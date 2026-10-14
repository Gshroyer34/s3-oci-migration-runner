"""Offline lab CA and CSR signing. Does not access cloud accounts.

CA passwords are prompted or read from a private file; never command-line values.
"""
import argparse
from datetime import datetime, timedelta, timezone
import getpass
import ipaddress
import json
import os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

PROFILES={'worker-tls':('migration-worker','tls'), 'worker-roles':('migration-worker','roles'),
    'writer-roles':('migration-fixture-writer','roles'), 'coordinator-tls':('migration-coordinator','tls'),
    'operator-tls':('migration-operator','tls'), 'database':('migration-db','tls')}


def write_new(path,data):
    path=Path(path)
    with path.open('xb') as stream: stream.write(data)
    path.chmod(0o600)


def initialize(directory,kind,password):
    if kind not in ('tls','roles') or len(password)<16: raise ValueError('Invalid CA inputs')
    directory=Path(directory); directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    key=ec.generate_private_key(ec.SECP256R1())
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'migration-lab-'+kind+'-ca')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=False,decipher_only=False),critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False)
        .sign(key,hashes.SHA256()))
    write_new(directory/'ca.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.BestAvailableEncryption(password)))
    write_new(directory/'ca.crt',cert.public_bytes(serialization.Encoding.PEM))


def request(directory,profile):
    directory=Path(directory); directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    key=ec.generate_private_key(ec.SECP256R1())
    csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,PROFILES[profile][0])])).sign(key,hashes.SHA256())
    write_new(directory/'client.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    write_new(directory/'client.csr',csr.public_bytes(serialization.Encoding.PEM))


def renew_authority(ca_dir, output, kind, password):
    """Reissue the lab's self-signed public CA with its existing encrypted key.

    Writes a new public certificate only; trust stores are updated separately.
    This is renewal, not key rotation or recovery from a compromised CA key.
    """
    if kind not in ('tls', 'roles'):
        raise ValueError('Invalid CA kind')
    ca_dir = Path(ca_dir)
    old = x509.load_pem_x509_certificate((ca_dir/'ca.crt').read_bytes())
    old.verify_directly_issued_by(old)
    expected = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'migration-lab-'+kind+'-ca')])
    if old.subject != expected or old.extensions.get_extension_for_class(x509.BasicConstraints).value != x509.BasicConstraints(ca=True, path_length=0):
        raise ValueError('Unexpected lab authority')
    key = serialization.load_pem_private_key((ca_dir/'ca.key').read_bytes(), password)
    public = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(old.public_key()) != public(key.public_key()):
        raise ValueError('CA key mismatch')
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(old.subject).issuer_name(old.issuer)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=30)))
    for extension in old.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    new = builder.sign(key, hashes.SHA256())
    new.verify_directly_issued_by(new)
    write_new(output, new.public_bytes(serialization.Encoding.PEM))


def issue(ca_dir,csr_path,output,profile,password, *, ca_certificate_path=None):
    ca_dir=Path(ca_dir); now=datetime.now(timezone.utc)
    ca=x509.load_pem_x509_certificate(Path(ca_certificate_path or ca_dir/'ca.crt').read_bytes())
    key=serialization.load_pem_private_key((ca_dir/'ca.key').read_bytes(),password=password)
    csr=x509.load_pem_x509_csr(Path(csr_path).read_bytes())
    if not csr.is_signature_valid: raise ValueError('Invalid CSR signature')
    expected=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,PROFILES[profile][0])])
    if csr.subject!=expected or ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value!='migration-lab-'+PROFILES[profile][1]+'-ca':
        raise ValueError('CSR or CA does not match the selected profile')
    if not now+timedelta(hours=24)<ca.not_valid_after_utc: raise ValueError('CA renewal required')
    builder=(x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject).public_key(csr.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,data_encipherment=False,key_agreement=False,key_cert_sign=False,crl_sign=False,encipher_only=False,decipher_only=False),critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if profile=='database' else ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False))
    if profile=='database': builder=builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('10.241.0.10'))]),critical=False)
    cert=builder.sign(key,hashes.SHA256())
    # Verify the loaded CA key corresponds to its public certificate before writing.
    cert.verify_directly_issued_by(ca)
    write_new(output,cert.public_bytes(serialization.Encoding.PEM))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init-ca'); init.add_argument('--kind',choices=['tls','roles'],required=True); init.add_argument('--output',required=True)
    req=sub.add_parser('request'); req.add_argument('--profile',choices=list(PROFILES),required=True); req.add_argument('--output',required=True)
    sign=sub.add_parser('issue'); sign.add_argument('--profile',choices=list(PROFILES),required=True); sign.add_argument('--ca',required=True); sign.add_argument('--csr',required=True); sign.add_argument('--output',required=True)
    for command in (init,sign): command.add_argument('--password-file')
    args=parser.parse_args()
    try:
        if args.action=='request': request(args.output,args.profile)
        else:
            password=Path(args.password_file).read_bytes().strip() if args.password_file else getpass.getpass('Offline CA passphrase: ').encode()
            if args.action=='init-ca': initialize(args.output,args.kind,password)
            else: issue(args.ca,args.csr,args.output,args.profile,password)
        print(json.dumps({'created':True,'cloud_contacted':False})); return 0
    except Exception:
        print('PKI operation rejected; check private paths, password, CSR identity and CA validity.'); return 1


if __name__=='__main__': raise SystemExit(main())
