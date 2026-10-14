"""Build, install and enroll a portable OCI pilot. Never starts a migration implicitly."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from . import enrollment, host_runtime as host
from .host_files import InstallError, atomic, encoded, lock, mkdir, protected, read, sha
from .host_plan import HostPlan, credential_policy
from .host_release import build_release


def output_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='action', required=True)
    ca = commands.add_parser('create-authorities', help='Offline only: create two encrypted authority keys')
    ca.add_argument('--name', required=True); ca.add_argument('--output', required=True, type=Path)
    token = commands.add_parser('create-token', help='Offline only: write a private deployment token; print its hash')
    token.add_argument('--output', required=True, type=Path)
    plan_command = commands.add_parser('make-plan', help='Combine deployment profile, exact node outputs and public authority fingerprints')
    for field in ('deployment', 'nodes', 'authorities', 'token-file', 'output'):
        plan_command.add_argument('--' + field, required=True, type=Path)
    plan_command.add_argument('--runtime-image', required=True)
    plan_command.add_argument('--features', type=Path, help='Optional reviewed UI and managed certificate settings')
    for name in ('validate-plan', 'credential-policy', 'build-release', 'sign'):
        command = commands.add_parser(name)
        command.add_argument('--plan', required=True, type=Path)
        if name != 'validate-plan':
            command.add_argument('--output', required=True, type=Path)
        if name == 'build-release':
            command.add_argument('--authorities', required=True, type=Path)
            command.add_argument('--source-root', required=True, type=Path)
        if name == 'sign':
            command.add_argument('--request', required=True, type=Path)
            command.add_argument('--authorities', required=True, type=Path)
    install = commands.add_parser('install')
    install.add_argument('--release', required=True, type=Path)
    install.add_argument('--expected-sha256', required=True)
    install.add_argument('--node', required=True)
    request = commands.add_parser('request', help='On node: generate keys and export public CSRs')
    request.add_argument('--output', required=True, type=Path)
    enroll = commands.add_parser('enroll', help='On node: validate signed leaves and conditionally update its Vault secret')
    enroll.add_argument('--signed', required=True, type=Path)
    enroll.add_argument('--token-file', required=True, type=Path)
    enroll.add_argument('--replace-digest', help='Prior credential digest required for deliberate same-plan renewal')
    for name in ('restore', 'initialize-ledger', 'status'):
        commands.add_parser(name)
    worker = commands.add_parser('worker', help='Run one explicitly selected reviewed pilot')
    worker.add_argument('--job', required=True)
    activate = commands.add_parser('activate-ui')
    activate.add_argument('--reference',type=Path)
    renew = commands.add_parser('renew')
    renew.add_argument('--token-file',type=Path,help='Required only for first managed enrollment')
    commands.add_parser('serve-ui')
    commands.add_parser('dispatch')
    execute = commands.add_parser('execute', help='Coordinator: pass arguments after -- to the scoped cloud CLI')
    execute.add_argument('arguments', nargs=argparse.REMAINDER)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'create-authorities':
            password = getpass.getpass('New authority passphrase (at least 12 characters): ')
            if password != getpass.getpass('Repeat authority passphrase: '):
                raise InstallError('Passphrases differ.')
            result = enrollment.create_authorities(args.output, args.name, password.encode())
        elif args.action == 'create-token':
            token = enrollment.new_token()
            output_new(args.output, token.encode())
            result = {'worker_token_sha256': sha(token.encode())}
        elif args.action == 'make-plan':
            from .deployment import read_profile
            token = read(args.token_file, 256).decode().strip()
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', token):
                raise InstallError('Use the generated deployment token.')
            features=json.loads(read(args.features)) if args.features else {}
            if set(features)-{'ui','managed_certificates'}:
                raise InstallError('Unexpected feature settings.')
            plan = HostPlan(**features,deployment=read_profile(args.deployment), nodes=json.loads(read(args.nodes)),
                runtime_image=args.runtime_image, worker_token_sha256=sha(token.encode()),
                tls_ca_sha256=enrollment.fingerprint(read(args.authorities / 'tls-ca.crt')),
                roles_ca_sha256=enrollment.fingerprint(read(args.authorities / 'roles-ca.crt')))
            output_new(args.output, plan.canonical())
            result = {'plan_revision': plan.revision, 'cloud_changed': False}
        elif args.action in ('validate-plan', 'credential-policy', 'build-release', 'sign'):
            plan = HostPlan.model_validate_json(read(args.plan))
            if args.action == 'validate-plan':
                result = {'valid': True, 'plan_revision': plan.revision, 'cloud_contacted': False}
            elif args.action == 'credential-policy':
                output_new(args.output, encoded(credential_policy(plan)))
                result = {'policy_written': True, 'cloud_changed': False}
            elif args.action == 'build-release':
                value = build_release(plan, read(args.authorities / 'tls-ca.crt'), read(args.authorities / 'roles-ca.crt'), args.source_root)
                output_new(args.output, value)
                result = {'release_sha256': sha(value), 'plan_revision': plan.revision, 'cloud_changed': False}
            else:
                signed = enrollment.sign_requests(plan, json.loads(read(args.request)), args.authorities,
                    getpass.getpass('Existing authority passphrase: ').encode())
                output_new(args.output, encoded(signed))
                result = {'signed': True, 'node_id': signed['node_id'], 'private_keys_exported': False}
        elif args.action == 'install':
            result = host.install(args.release, args.expected_sha256, args.node)
        else:
            plan, node = host.load()
            lock_name={'restore':'credentials.lock','worker':'worker-operation.lock','serve-ui':'ui-process.lock'}.get(args.action,'operation.lock')
            with lock(host.STATE,lock_name):
                if args.action == 'status':
                    result = json.loads(read(host.STATE / 'checkpoint.json'))
                    try:
                        result['credentials'] = host.ready(plan, node)
                    except (OSError, ValueError, KeyError):
                        result['credentials'] = {'ready': False}
                    result['cloud_contacted'] = False
                else:
                    host.instance_identity(plan, node)
                    if args.action in ('activate-ui','serve-ui','dispatch','renew'):
                        from . import portable_host
                        if args.action=='activate-ui': result=portable_host.activate(plan,node,args.reference)
                        elif args.action=='serve-ui': return portable_host.serve(plan,node)
                        elif args.action=='dispatch': result=portable_host.dispatch(plan,node)
                        else: result=portable_host.renew(plan,node,args.token_file)
                    elif args.action == 'request':
                        directory = mkdir(host.STATE / 'enrollment')
                        request = enrollment.request_credentials(plan, node.node_id, directory)
                        output_new(args.output, encoded(request))
                        result = {'request_created': True, 'node_id': node.node_id, 'private_keys_exported': False}
                    elif args.action == 'enroll':
                        protected(args.token_file)
                        if args.token_file.stat().st_mode & 0o077:
                            raise InstallError('Token file must be accessible only to root.')
                        bundle = enrollment.assemble(plan, node.node_id, json.loads(read(args.signed)),
                            protected(host.STATE / 'enrollment', directory=True), read(args.token_file, 256).decode().strip())
                        reader, writer = host.clients(plan)
                        result = enrollment.enroll(plan, node.node_id, bundle, reader, writer,
                            read(host.CONFIG / 'tls-ca.crt'), read(host.CONFIG / 'roles-ca.crt'), args.replace_digest)
                        atomic(host.STATE / 'enrollment-receipt.json', encoded(result))
                        atomic(host.STATE / 'checkpoint.json', encoded({'stage': 'enrolled', 'plan_revision': plan.revision, **result}))
                    elif args.action == 'restore':
                        host.restore(plan, node)
                        result = {'credentials_restored': True, **host.ready(plan, node)}
                    elif args.action == 'initialize-ledger':
                        result = host.initialize(plan, node)
                    elif args.action == 'worker':
                        return host.execute(plan, node, [], job=args.job)
                    else:
                        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
                        if not arguments:
                            raise InstallError('Provide an explicit scoped coordinator command after --.')
                        return host.execute(plan, node, arguments)
        print(json.dumps(result, sort_keys=True))
        return 0
    except InstallError as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
    except Exception:
        # Validation, crypto, cloud SDK and subprocess exceptions can include
        # private submitted values. Details never leave this credential boundary.
        print(json.dumps({'error': 'Operation failed. Check inputs, node identity, certificate validity, IAM and the saved checkpoint. No credential values are logged.'}), file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
