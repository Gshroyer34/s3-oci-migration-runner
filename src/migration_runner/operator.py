"""Authenticated localhost CLI. Credentials come from configuration, never arguments."""
import argparse
import json
import urllib.request
import urllib.error
from uuid import UUID
from migration_runner.config import Settings


def main():
    parser=argparse.ArgumentParser(description='Local operator API client')
    parser.add_argument('action',choices=['jobs','preview','start','pause','drain','resume','cancel','retry','export','audit'])
    parser.add_argument('--job',type=UUID)
    parser.add_argument('--batch',type=int)
    parser.add_argument('--settings-file',help='JSON start request with roots and limits; no credentials')
    args=parser.parse_args()
    if args.action not in ('jobs','audit') and not args.job:
        parser.error('--job is required')
    path='/api/'+args.action if args.action in ('jobs','audit') else f'/api/jobs/{args.job}'
    data=None
    if args.action not in ('jobs','audit','preview'):
        path+='/'+args.action
        if args.action!='export': data=b'{}'
    if args.action=='retry':
        if not args.batch or args.batch<1: parser.error('--batch must be positive')
        path+='/'+str(args.batch)
    if args.action=='start':
        if not args.settings_file: parser.error('--settings-file is required')
        from pathlib import Path
        data=json.dumps(json.loads(Path(args.settings_file).read_text())).encode()
    request=urllib.request.Request('http://127.0.0.1:8000'+path,data=data,headers={'Authorization':'Bearer '+Settings().operator_token.get_secret_value(),'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=130) as response:
            print(response.read().decode())
    except (urllib.error.URLError,TimeoutError):
        raise SystemExit('Operator request failed; check local service, credentials and job state')


if __name__=='__main__':
    main()
