"""Versioned, nonsecret installation inputs shared by CLI and HTTP clients."""
import base64
import hashlib
import ipaddress
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


def identifier(value, kind):
    if not re.fullmatch(r'ocid1\.' + kind + r'\.oc1\.[a-zA-Z0-9._-]{16,220}', value):
        raise ValueError('Enter a commercial OCI resource identifier of the required type.')
    return value


def prefix(value):
    # IAM wildcards and traversal are intentionally excluded from this initial envelope.
    if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,256}/', value)
            or any(p in ('', '.', '..') for p in value[:-1].split('/'))):
        raise ValueError('Use a dedicated ASCII prefix ending in /, without wildcards or traversal.')
    return value


class Source(InputModel):
    account_id: str = Field(pattern=r'^\d{12}$')
    region: str = Field(pattern=r'^(us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+$', max_length=40)
    bucket: str = Field(pattern=r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
    prefix: str
    kms_key_arn: str | None = None

    @field_validator('bucket')
    @classmethod
    def bucket_name(cls, value):
        if '..' in value or '.-' in value or '-.' in value or re.fullmatch(r'\d+\.\d+\.\d+\.\d+', value):
            raise ValueError('Enter a supported S3 bucket name.')
        return value

    _prefix = field_validator('prefix')(prefix)

    @model_validator(mode='after')
    def kms_scope(self):
        if self.kms_key_arn and not re.fullmatch(
                rf'arn:aws:kms:{re.escape(self.region)}:{self.account_id}:key/[A-Za-z0-9-]{{1,128}}', self.kms_key_arn):
            raise ValueError('The KMS key must belong to the selected source account and region.')
        return self


class Destination(InputModel):
    tenancy_ocid: str
    compartment_ocid: str
    region: str = Field(pattern=r'^[a-z]{2}-[a-z]+-\d+$', max_length=40)
    namespace: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    bucket: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    prefix: str

    _prefix = field_validator('prefix')(prefix)

    @field_validator('tenancy_ocid', 'compartment_ocid')
    @classmethod
    def ocid(cls, value, info):
        return identifier(value, info.field_name.removesuffix('_ocid'))


class Network(InputModel):
    vcn_ocid: str
    subnet_ocid: str
    admin_cidr: str
    payload_route: Literal['internet', 'private']
    s3_endpoint_id: str | None = Field(default=None, pattern=r'^vpce-[0-9a-f]{8,32}$')

    @field_validator('vcn_ocid', 'subnet_ocid')
    @classmethod
    def ocid(cls, value, info):
        return identifier(value, info.field_name.removesuffix('_ocid'))

    @field_validator('admin_cidr')
    @classmethod
    def cidr(cls, value):
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or network.prefixlen < 24:
            raise ValueError('Use an IPv4 administrator range of /24 or narrower.')
        return str(network)

    @model_validator(mode='after')
    def path(self):
        if (self.payload_route == 'private') != bool(self.s3_endpoint_id):
            raise ValueError('Private transport requires an S3 interface endpoint ID; internet transport must omit it.')
        return self


class Compute(InputModel):
    availability_domain: str = Field(pattern=r'^[A-Za-z0-9:_.-]{1,100}$')
    image_ocid: str
    shape: Literal['VM.Standard.E4.Flex', 'VM.Standard.E5.Flex', 'VM.Optimized3.Flex'] = 'VM.Standard.E5.Flex'
    ocpus: int = Field(default=2, ge=1, le=16)
    memory_gib: int = Field(default=16, ge=8, le=256)
    initial_workers: int = Field(default=2, ge=1, le=4)
    maximum_workers: int = Field(default=4, ge=1, le=4)
    ssh_public_key: str = Field(min_length=60, max_length=4096)

    @field_validator('image_ocid')
    @classmethod
    def image(cls, value):
        return identifier(value, 'image')

    @field_validator('ssh_public_key')
    @classmethod
    def public_key(cls, value):
        parts = value.split()
        if '\n' in value or '\r' in value or len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-rsa'):
            raise ValueError('Supply a single OpenSSH public key, never a private key.')
        try:
            data = base64.b64decode(parts[1], validate=True)
            offset = 0
            def take():
                nonlocal offset
                if offset + 4 > len(data):
                    raise ValueError()
                size = int.from_bytes(data[offset:offset + 4], 'big')
                offset += 4
                if size < 1 or offset + size > len(data):
                    raise ValueError()
                item = data[offset:offset + size]
                offset += size
                return item
            if take().decode() != parts[0]:
                raise ValueError()
            if parts[0] == 'ssh-ed25519':
                if len(take()) != 32:
                    raise ValueError()
            else:
                exponent, modulus = int.from_bytes(take(), 'big'), int.from_bytes(take(), 'big')
                if exponent < 3 or exponent % 2 == 0 or not 2048 <= modulus.bit_length() <= 8192:
                    raise ValueError()
            if offset != len(data):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError('Invalid OpenSSH public key encoding.') from None
        # Comments are not needed for authorization and may contain pasted secrets.
        return ' '.join(parts[:2])

    @model_validator(mode='after')
    def bounds(self):
        if self.initial_workers > self.maximum_workers:
            raise ValueError('Initial workers cannot exceed the deployment maximum.')
        if self.memory_gib < self.ocpus or self.memory_gib > self.ocpus * 64:
            raise ValueError('Memory must be between 1 and 64 GiB per OCPU; cloud shape validation is also required.')
        return self


class InstallationProfile(InputModel):
    schema_version: Literal['migration-installation/v1'] = 'migration-installation/v1'
    deployment_name: str = Field(pattern=r'^[a-z][a-z0-9-]{2,23}$')
    source: Source
    destination: Destination
    network: Network
    compute: Compute

    @property
    def revision(self):
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(canonical).hexdigest()

    def report(self):
        return {
            'configuration_valid': True, 'profile_revision': self.revision,
            'stage': 'infrastructure_foundation', 'cloud_validation': 'not_performed',
            'runtime_activation_supported': False, 'automatic_scaling_supported': False,
            'summary': {'deployment_name': self.deployment_name,
                        'initial_instances': self.compute.initial_workers + 1,
                        'total_initial_ocpus': 2 + self.compute.initial_workers * self.compute.ocpus,
                        'payload_route': self.network.payload_route},
            'required_checks': [
                'Confirm target tenancy, compartment and AWS account with their administrators.',
                'Confirm the existing subnet is private and has approved routing, DNS and service access.',
                'Confirm the Oracle Linux 9 x86 image supports the selected shape and capacity is available.',
                'Review infrastructure cost and retention; worker maximum is not a billing cutoff.',
                'Provide a deployment-owned public CA certificate for AWS workload trust.',
                'Complete runtime release installation, Vault enrollment and certificate lifecycle integration.',
                'Validate source read, destination write and the actual network path before admitting jobs.',
            ],
        }
