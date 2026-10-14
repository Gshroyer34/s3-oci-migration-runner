"""Nonsecret, immutable inputs for the portable Linux pilot installer."""
import hashlib
import ipaddress
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .deployment import DeploymentProfile
from .profile import InputModel, identifier

POSTGRES = 'docker.io/library/postgres:17.11-bookworm@sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6'
SCHEMA = '0007_portable_workers'


class OperatorUI(InputModel):
    reference_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    max_runs: int = Field(default=3, ge=1, le=10)


class ManagedCertificate(InputModel):
    node_id: str = Field(pattern=r'^(coordinator|worker-[1-4])$')
    purpose: Literal['tls', 'roles', 'database']
    certificate_id: str
    authority_id: str

    _certificate = field_validator('certificate_id')(lambda value: identifier(value, 'certificate'))
    _authority = field_validator('authority_id')(lambda value: identifier(value, 'certificateauthority'))


class HostNode(InputModel):
    node_id: str = Field(pattern=r'^(coordinator|worker-[1-4])$')
    instance_id: str
    private_ip: str
    secret_id: str

    @field_validator('instance_id')
    @classmethod
    def instance(cls, value):
        return identifier(value, 'instance')

    @field_validator('secret_id')
    @classmethod
    def secret(cls, value):
        return identifier(value, 'vaultsecret')

    @field_validator('private_ip')
    @classmethod
    def address(cls, value):
        address = ipaddress.IPv4Address(value)
        if not any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('Use an RFC1918 private IPv4 address.')
        return str(address)


class HostPlan(InputModel):
    schema_version: Literal['migration-host-installation/v1'] = 'migration-host-installation/v1'
    deployment: DeploymentProfile
    runtime_image: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    tls_ca_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    roles_ca_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    worker_token_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    nodes: list[HostNode] = Field(min_length=2, max_length=5)
    ui: OperatorUI | None = None
    managed_certificates: list[ManagedCertificate] | None = None

    @model_validator(mode='after')
    def topology(self):
        if sum(n.node_id == 'coordinator' for n in self.nodes) != 1:
            raise ValueError('Exactly one coordinator is required.')
        if len(self.nodes) - 1 > self.deployment.limits.max_workers:
            raise ValueError('Worker count exceeds the approved deployment.')
        for field in ('node_id', 'instance_id', 'private_ip', 'secret_id'):
            if len({getattr(n, field) for n in self.nodes}) != len(self.nodes):
                raise ValueError('Each node must have a distinct identity, address and secret.')
        if self.tls_ca_sha256 == self.roles_ca_sha256:
            raise ValueError('Use separate AWS and transport authorities.')
        if self.ui and {node.node_id for node in self.nodes if node.node_id!='coordinator'} != {f'worker-{number}' for number in range(1,len(self.nodes))}:
            raise ValueError('UI dispatch requires consecutive worker IDs starting at worker-1.')
        if self.managed_certificates is not None:
            expected = {(node.node_id, purpose) for node in self.nodes for purpose in self.purposes(node.node_id)}
            actual = {(entry.node_id, entry.purpose) for entry in self.managed_certificates}
            if actual != expected or len(actual) != len(self.managed_certificates) or len({e.certificate_id for e in self.managed_certificates}) != len(actual):
                raise ValueError('Assign one distinct managed certificate to each node and purpose.')
            tls = {e.authority_id for e in self.managed_certificates if e.purpose != 'roles'}
            roles = {e.authority_id for e in self.managed_certificates if e.purpose == 'roles'}
            if len(tls) != 1 or len(roles) != 1 or tls == roles:
                raise ValueError('Use one dedicated transport authority and a separate AWS authority.')
        return self

    def canonical(self):
        value = self.model_dump()
        for name in ('ui', 'managed_certificates'):
            if value[name] is None:
                value.pop(name)
        return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()

    @property
    def revision(self):
        return hashlib.sha256(self.canonical()).hexdigest()

    def node(self, node_id):
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise ValueError('Node is not in this installation plan.')

    def common_name(self, node_id, purpose):
        suffix = 'worker' if purpose == 'roles' else 'database' if purpose == 'database' else node_id
        return 'migration-' + self.deployment.deployment_name + '-' + suffix

    def purposes(self, node_id):
        self.node(node_id)
        return ('tls', 'roles', 'database') if node_id == 'coordinator' else ('tls', 'roles')


def credential_policy(plan):
    """Reviewable exact-instance/exact-secret OCI IAM statements; never applies IAM."""
    compartment = plan.deployment.destination.compartment_ocid
    entries = []
    for node in plan.nodes:
        group = plan.deployment.deployment_name + '-' + node.node_id
        scope = f"in compartment id {compartment} where target.secret.id = '{node.secret_id}'"
        entries.append({
            'node_id': node.node_id, 'dynamic_group_name': group,
            'matching_rule': f"ALL {{instance.id = '{node.instance_id}'}}",
            'steady_state_policy': f'Allow dynamic-group {group} to read secret-bundles {scope}',
            'temporary_enrollment_policy': f"Allow dynamic-group {group} to manage secrets in compartment id {compartment} where all {{target.secret.id = '{node.secret_id}', any {{request.permission = 'SECRET_READ', request.permission = 'SECRET_UPDATE'}}}}",
            'remove_temporary_policy_after_readback': True,
            'initial_secret_payload': {'format': 'migration-enrollment-pending/v1',
                                       'plan_revision': plan.revision, 'node_id': node.node_id},
        })
    for value,node in zip(entries,plan.nodes):
        value['renewal_policies'] = []
        if plan.managed_certificates:
            value['renewal_policies'].append(value['temporary_enrollment_policy'])
            value['remove_temporary_policy_after_readback'] = False
            for certificate in plan.managed_certificates:
                if certificate.node_id != node.node_id: continue
                group=value['dynamic_group_name']
                value['renewal_policies'].extend([
                    f"Allow dynamic-group {group} to use leaf-certificates in compartment id {compartment} where target.leaf-certificate.id = '{certificate.certificate_id}'",
                    f"Allow dynamic-group {group} to read leaf-certificate-bundles in compartment id {compartment} where all {{target.leaf-certificate.id = '{certificate.certificate_id}', target.leaf-certificate.bundle-type = 'CERTIFICATE_CONTENT_PUBLIC_ONLY'}}",
                    f"Allow dynamic-group {group} to use certificate-authority-delegates in compartment id {compartment} where target.certificate-authority.id = '{certificate.authority_id}'"])
            value['issuer_prerequisites'] = 'Administrator must configure the dedicated OCI issuer CA, signing key, CRL bucket and service permissions required by UpdateCertificate. These statements are not a complete issuer provisioning policy.'
    return {'format': 'migration-credential-policy/v1', 'nodes': entries,
            'notes': 'Create each secret in this compartment with its placeholder payload. No private key or token belongs in Terraform state. Destination object permissions and network rules are separate prerequisites.'}
