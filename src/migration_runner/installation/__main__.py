"""Installation preparation and optional read-only connection checks. Never applies a stack."""
import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .package import build_package
from .profile import InstallationProfile
from .deployment import DeploymentError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'package', 'schema', 'serve', 'runtime-package', 'install-profile'])
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--port', type=int, default=8793)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--live-connections', action='store_true', help='Enable read-only checks with existing local cloud profiles')
    parser.add_argument('--live-discovery', action='store_true', help='Enable read-only, checkpointed S3 discovery')
    parser.add_argument('--discovery-state-dir', type=Path, help='Private local directory for object observations and checkpoints')
    parser.add_argument('--aws-profile', help='Existing AWS CLI profile used only by live checks')
    parser.add_argument('--oci-profile', help='Existing OCI API-key profile used only by live checks')
    parser.add_argument('--connection-settings', type=Path, help='Optional nonsecret connection settings to prefill the form')
    parser.add_argument('--reader-identity', type=Path, help='Nonsecret AWS role, profile and trust-anchor ARNs from the deployment stack')
    parser.add_argument('--destination-run-prefix', help='Dedicated pilot run prefix below the deployment destination prefix')
    parser.add_argument('--expected-revision', help='Reviewed SHA-256 deployment revision required for Linux profile publication')
    args = parser.parse_args()
    if args.live_connections and (args.action != 'serve' or not args.aws_profile or not args.oci_profile):
        parser.error('Live checks require serve, --aws-profile and --oci-profile.')
    if args.live_discovery and (args.action != 'serve' or not args.aws_profile or not args.discovery_state_dir):
        parser.error('Discovery requires serve, --aws-profile and --discovery-state-dir.')
    if args.discovery_state_dir and not args.live_discovery:
        parser.error('--discovery-state-dir requires --live-discovery.')
    if not args.live_connections and (args.oci_profile or args.connection_settings or (args.aws_profile and not args.live_discovery)):
        parser.error('Cloud profile options require --live-connections.')
    if args.action == 'serve':
        if not 1024 <= args.port <= 65535:
            parser.error('Choose a local port from 1024 to 65535.')
        import threading
        import webbrowser
        import uvicorn
        from .web import create_app
        tester = None
        if args.live_connections:
            from .connections import ConnectionSettings, LocalConnectionTester
            defaults = None
            if args.connection_settings:
                try:
                    if args.connection_settings.stat().st_size > 16384:
                        raise ValueError()
                    defaults = ConnectionSettings.model_validate(json.loads(args.connection_settings.read_text(encoding='utf-8-sig')))
                except (OSError, ValueError):
                    parser.error('Connection settings must be a valid nonsecret connection profile under 16 KiB.')
            tester = LocalConnectionTester(args.aws_profile, args.oci_profile, defaults=defaults)
        if not args.no_browser:
            threading.Timer(1, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}/static/install.html')).start()
        discovery = None
        if args.live_discovery:
            from .discovery import AWSDiscoveryClient, DiscoveryService
            discovery = DiscoveryService(args.discovery_state_dir, AWSDiscoveryClient(args.aws_profile))
        uvicorn.run(create_app(tester, discovery), host='127.0.0.1', port=args.port, access_log=False, proxy_headers=False)
        return 0
    if args.action == 'schema':
        print(json.dumps(InstallationProfile.model_json_schema(), indent=2))
        return 0
    if not args.profile or (args.action in ('package', 'runtime-package', 'install-profile') and not args.output):
        parser.error('--profile is required; packaging and installation also require --output')
    if args.action == 'runtime-package' and (not args.reader_identity or not args.destination_run_prefix):
        parser.error('Runtime packaging requires --reader-identity and --destination-run-prefix.')
    if args.action == 'install-profile' and not args.expected_revision:
        parser.error('Profile publication requires --expected-revision.')
    try:
        if args.action == 'install-profile':
            from .deployment import read_profile, publish_profile
            profile = read_profile(args.profile)
            if profile.revision != args.expected_revision:
                raise DeploymentError('Revision mismatch.')
            path = publish_profile(profile, args.output)
            print(json.dumps({'profile_installed': True, 'deployment_revision': profile.revision,
                              'profile_path': str(path), 'services_activated': False}))
            return 0
        if args.profile.stat().st_size > 16384:
            raise ValueError('Profile exceeds 16 KiB.')
        profile = InstallationProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8-sig')))
        if args.action == 'validate':
            print(json.dumps(profile.report(), indent=2))
        elif args.action == 'runtime-package':
            from .deployment import AWSReaderIdentity
            from .package import build_runtime_package
            if args.reader_identity.stat().st_size > 4096:
                raise ValueError('Reader identity input too large.')
            identity = AWSReaderIdentity.model_validate_json(args.reader_identity.read_bytes())
            content = build_runtime_package(profile, identity, args.destination_run_prefix)
            with args.output.open('xb') as output:
                output.write(content)
            print('Portable pilot profile package created. No cloud changes or service activation performed.')
        else:
            # Never overwrite a reviewed release or follow an existing output symlink.
            with args.output.open('xb') as output:
                output.write(build_package(profile))
            print('Installation foundation package created. No cloud changes made; runtime activation is not included.')
        return 0
    except ValidationError as error:
        paths = ['.'.join(map(str, e['loc'])) for e in error.errors(include_input=False)]
        print('Invalid installation fields: ' + ', '.join(paths), file=sys.stderr)
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError):
        print('Check the profile and output paths. Existing files were not overwritten.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
