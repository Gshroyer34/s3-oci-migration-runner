"""Assemble a validated node bundle in a NEW private file; never upload or print it."""
import argparse,json
from pathlib import Path
from migration_runner.runtime import Node,read_bounded,validate_bundle

parser=argparse.ArgumentParser(description=__doc__)
for name in ('node','trust','tls-directory','roles-directory','token-file','output'):
    parser.add_argument('--'+name,required=True,type=Path)
a=parser.parse_args()
try:
    node=Node.model_validate_json(read_bounded(a.node))
    data={'version':1,'node_id':node.node_id,'worker_token':read_bounded(a.token_file,1024).decode().strip()}
    for kind,directory in (('tls',a.tls_directory),('roles',a.roles_directory)):
        data[kind+'_cert']=read_bounded(directory/'client.crt').decode()
        data[kind+'_key']=read_bounded(directory/'client.key').decode()
    validate_bundle(data,node,a.trust)
    with a.output.open('x',encoding='utf-8') as stream: json.dump(data,stream)
    a.output.chmod(0o600)
    print(json.dumps({'bundle_created':True,'uploaded':False}))
except Exception:
    raise SystemExit('Bundle assembly rejected; verify private paths, node identity, certificates and enrollment token') from None
