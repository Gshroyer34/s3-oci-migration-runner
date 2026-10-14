"""Create an allowlisted, checksummed installation archive entirely offline."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

from .aws import reader_template
from .profile import InstallationProfile
from migration_runner.worker.properties import CONTRACT

TEMPLATES = Path(__file__).with_name('templates')
FILES = ('versions.tf', 'variables.tf', 'main.tf', 'cloud-init.yaml', '.terraform.lock.hcl')


def encode(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            # Stable timestamps let the same profile and release reproduce identical bytes.
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            bundle.writestr(info, data)
    return output.getvalue()


def terraform_values(p):
    return {
        'deployment_name': p.deployment_name, 'profile_revision': p.revision,
        'tenancy_ocid': p.destination.tenancy_ocid, 'compartment_ocid': p.destination.compartment_ocid,
        'region': p.destination.region, 'vcn_ocid': p.network.vcn_ocid, 'subnet_ocid': p.network.subnet_ocid,
        'availability_domain': p.compute.availability_domain, 'image_ocid': p.compute.image_ocid,
        'worker_shape': p.compute.shape, 'worker_ocpus': p.compute.ocpus, 'worker_memory_gib': p.compute.memory_gib,
        'initial_workers': p.compute.initial_workers, 'maximum_workers': p.compute.maximum_workers,
        'admin_cidr': p.network.admin_cidr, 'ssh_public_key': p.compute.ssh_public_key,
        'acknowledge_foundation_only': False,
    }


def resource_manager_schema(p):
    variables = {key: {'title': key.replace('_', ' ').capitalize(), 'type': 'string', 'required': True, 'default': value}
                 for key, value in terraform_values(p).items()}
    for key, title, kind in (
        ('tenancy_ocid', 'OCI tenancy', 'string'),
        ('compartment_ocid', 'Installation compartment', 'oci:identity:compartment:id'),
        ('region', 'OCI region', 'oci:identity:region:name'),
        ('vcn_ocid', 'Existing VCN', 'oci:core:vcn:id'),
        ('subnet_ocid', 'Existing private subnet', 'oci:core:subnet:id'),
        ('availability_domain', 'Availability domain', 'oci:identity:availabilitydomain:name'),
        ('image_ocid', 'Oracle Linux 9 x86 image', 'oci:core:image:id'),
        ('ssh_public_key', 'Administrator SSH public key', 'oci:core:ssh:publickey'),
    ):
        variables[key].update(title=title, type=kind)
    for key in ('vcn_ocid', 'subnet_ocid', 'availability_domain', 'image_ocid'):
        variables[key]['dependsOn'] = {'compartmentId': '${compartment_ocid}'}
    variables['subnet_ocid']['dependsOn']['vcnId'] = '${vcn_ocid}'
    variables['image_ocid']['dependsOn'].update(operatingSystem='Oracle Linux', operatingSystemVersion='9')
    variables['worker_shape'].update(type='enum', enum=['VM.Standard.E4.Flex', 'VM.Standard.E5.Flex', 'VM.Optimized3.Flex'])
    for key, maximum in [('initial_workers', 4), ('maximum_workers', 4), ('worker_ocpus', 16), ('worker_memory_gib', 256)]:
        variables[key].update(type='integer', minimum=8 if key == 'worker_memory_gib' else 1, maximum=maximum)
    variables['acknowledge_foundation_only'].update(type='boolean', title='I understand this provisions infrastructure only',
        description='Billable private compute and Vault resources. The migration application is not installed by this release.')
    variables['profile_revision']['visible'] = False
    return {
        'title': 'Migration Runner infrastructure foundation', 'schemaVersion': '1.1.0', 'locale': 'en',
        'description': 'Prepare private OCI infrastructure. Runtime installation and credential enrollment are separate, unfinished stages.',
        'variableGroups': [
            {'title': 'Deployment', 'variables': ['deployment_name', 'tenancy_ocid', 'compartment_ocid', 'region', 'profile_revision']},
            {'title': 'Private network and access', 'variables': ['vcn_ocid', 'subnet_ocid', 'admin_cidr', 'ssh_public_key']},
            {'title': 'Compute', 'variables': ['availability_domain', 'image_ocid', 'worker_shape', 'worker_ocpus', 'worker_memory_gib', 'initial_workers', 'maximum_workers']},
            {'title': 'Review before provisioning', 'variables': ['acknowledge_foundation_only']},
        ], 'variables': variables,
    }


def build_package(profile: InstallationProfile):
    foundation = {name: (TEMPLATES / name).read_bytes() for name in FILES}
    foundation['schema.yaml'] = encode(resource_manager_schema(profile))  # JSON is valid YAML.
    foundation['installation.auto.tfvars.json'] = encode(terraform_values(profile))
    foundation['README.md'] = (TEMPLATES / 'START_HERE.md').read_bytes()
    files = {
        'START_HERE.md': (TEMPLATES / 'START_HERE.md').read_bytes(),
        'PROPERTY_COMPATIBILITY.md': (TEMPLATES / 'PROPERTY_COMPATIBILITY.md').read_bytes(),
        'property-mapping.json': encode(CONTRACT),
        'installation-profile.json': encode(profile.model_dump()),
        'configuration-validation.json': encode(profile.report()),
        'oci-foundation.zip': archive(foundation),
        'aws-source-reader.cloudformation.json': encode(reader_template(profile)),
    }
    manifest = {'format': 'migration-installation-package/v1', 'release': 'foundation-preview-1',
                'profile_revision': profile.revision, 'runtime_activation_supported': False,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files['manifest.json'] = encode(manifest)
    return archive(files)


def build_runtime_package(installation, reader_identity, destination_run_prefix):
    from .deployment import DeploymentProfile
    profile = DeploymentProfile.from_installation(installation, reader_identity)
    binding = profile.binding(destination_run_prefix)
    profile.authorize(binding)
    files = {
        'START_HERE.md': (TEMPLATES / 'RUNTIME_PROFILE.md').read_bytes(),
        'PROPERTY_COMPATIBILITY.md': (TEMPLATES / 'PROPERTY_COMPATIBILITY.md').read_bytes(),
        'property-mapping.json': encode(CONTRACT),
        'deployment-profile.json': profile.canonical(),
        'binding.json': encode(binding.model_dump()),
        'aws-config.template': profile.aws_config().encode(),
        'deployment.env.example': (
            'MIGRATION_DEPLOYMENT_PROFILE=/etc/migration/deployments/' + profile.revision + '.json\n'
            'MIGRATION_DEPLOYMENT_REVISION=' + profile.revision + '\n').encode(),
    }
    files['manifest.json'] = encode({
        'format': 'migration-runtime-profile-package/v1', 'release': 'profile-binding-preview-1',
        'deployment_revision': profile.revision, 'installation_revision': installation.revision,
        'roles_certificate_common_name': 'migration-' + profile.deployment_name + '-worker',
        'automatic_installation_supported': False, 'cloud_validation': 'not_performed',
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    })
    return archive(files)
