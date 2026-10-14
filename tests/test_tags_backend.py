"""Actual pinned Rclone against loopback HTTP contracts; no cloud credentials."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import sys
from threading import Thread

import pytest

from migration_runner.worker.cloud import CloudRclone
from migration_runner.worker.tags import TAG_METADATA_KEY, decode_tags, encode_tags
from test_cloud import binding


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('rclone'), reason='Pinned OL9 Rclone required')
@pytest.mark.parametrize('multipart',[False,True])
def test_real_rclone_metadata_roundtrip_single_and_multipart(monkeypatch,multipart):
    payload=b'synthetic-tag-copy\x00'*(1_000_000 if multipart else 100)
    tags={'Owner':'Finance','owner':'','分類':'記録 😀'}
    state={'payload':None,'metadata':{},'parts':{},'multipart':False}
    calls=[]
    errors=[]
    native={'Content-Type':'application/octet-stream', 'Content-Encoding':'identity',
            'Content-Disposition':'attachment; filename="example,1.bin"',
            'Content-Language':'en-US', 'Cache-Control':'max-age=60, private'}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def reply(self,status,body=b'',headers=None,head=False):
            self.send_response(status)
            for k,v in (headers or {}).items(): self.send_header(k,v)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            if not head: self.wfile.write(body)
        def source(self,head=False):
            calls.append(('source',self.command))
            self.reply(200,payload,{'ETag':'"'+hashlib.md5(payload).hexdigest()+'"',
                'Last-Modified':'Fri, 11 Sep 2026 00:00:00 GMT','Content-Type':'application/octet-stream',
                'x-amz-meta-existing':'original', **native},head)
        def destination(self,head=False):
            calls.append(('destination',self.command))
            if state['payload'] is None:
                self.reply(404,b'{"code":"ObjectNotFound","message":"missing"}',head=head); return
            headers={'ETag':'destination-etag','Last-Modified':'Tue, 13 Oct 2026 00:00:00 GMT',
                'Content-Type':'application/octet-stream',
                'content-md5':base64.b64encode(hashlib.md5(state['payload']).digest()).decode()}
            headers.update({'opc-meta-'+k:v for k,v in state['metadata'].items()})
            self.reply(200,state['payload'],headers,head)
        def do_HEAD(self):
            if self.path.startswith('/n/'): self.destination(True)
            else: self.source(True)
        def do_GET(self):
            if self.path.startswith('/n/'): self.destination()
            else: self.source()
        def do_PUT(self):
            calls.append(('put', self.path))
            body=self.rfile.read(int(self.headers['Content-Length']))
            if '/u/' in self.path:
                from urllib.parse import urlparse,parse_qs
                part=int(parse_qs(urlparse(self.path).query)['uploadPartNum'][0])
                state['parts'][part]=body
                self.reply(200,headers={'ETag':str(part),'opc-content-md5':base64.b64encode(hashlib.md5(body).digest()).decode()})
            else:
                state['payload']=body
                state['metadata']={k.lower()[9:]:v for k,v in self.headers.items() if k.lower().startswith('opc-meta-')}
                state['native']={k:self.headers.get(k) for k in native}
                self.reply(200,headers={'ETag':'destination-etag','opc-content-md5':base64.b64encode(hashlib.md5(body).digest()).decode()})
        def do_POST(self):
            calls.append(('post', self.path))
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/u'):
                state['multipart']=True
                # OCI multipart metadata uses opc-meta- prefixed map keys.
                state['metadata']={k.removeprefix('opc-meta-'):v for k,v in data.get('metadata',{}).items()}
                names={'Content-Type':'contentType','Content-Encoding':'contentEncoding',
                       'Content-Disposition':'contentDisposition','Content-Language':'contentLanguage','Cache-Control':'cacheControl'}
                state['native']={k:data.get(v) for k,v in names.items()}
                self.reply(200,json.dumps({'uploadId':'upload-one','object':'pilot/run1/object-001.bin'}).encode(),{'Content-Type':'application/json'})
            else:
                parts=[state['parts'][int(p['partNum'])] for p in sorted(data['partsToCommit'],key=lambda p:int(p['partNum']))]
                state['payload']=b''.join(parts)
                digest=hashlib.md5(b''.join(hashlib.md5(p).digest() for p in parts)).digest()
                self.reply(200,headers={'ETag':'destination-etag','opc-multipart-md5':base64.b64encode(digest).decode()+'-'+str(len(parts))})
        def do_DELETE(self): self.reply(204)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile',lambda _: '/nonexistent')
    a=CloudRclone(binding(),'ignored')
    a.env.update({'RCLONE_CONFIG_SOURCE_ENDPOINT':f'http://127.0.0.1:{server.server_port}',
        'RCLONE_CONFIG_SOURCE_FORCE_PATH_STYLE':'true','RCLONE_CONFIG_SOURCE_ENV_AUTH':'false',
        'RCLONE_CONFIG_SOURCE_ACCESS_KEY_ID':'TESTONLY','RCLONE_CONFIG_SOURCE_SECRET_ACCESS_KEY':'TESTONLY',
        'RCLONE_CONFIG_DESTINATION_PROVIDER':'no_auth',
        'RCLONE_CONFIG_DESTINATION_ENDPOINT':f'http://127.0.0.1:{server.server_port}',
        'RCLONE_CONFIG_DESTINATION_UPLOAD_CUTOFF':'16Mi'})
    a.env.pop('RCLONE_CONFIG_SOURCE_PROFILE')
    spawn=a.spawn
    class CapturedPipe:
        def __init__(self,pipe): self.pipe=pipe
        def readline(self,*args):
            line=self.pipe.readline(*args)
            errors.append(line.decode('utf-8',errors='replace'))
            return line
        def close(self): self.pipe.close()
    def capture(*args,**kwargs):
        process=spawn(*args,**kwargs)
        if process.stderr: process.stderr=CapturedPipe(process.stderr)
        return process
    monkeypatch.setattr(a,'spawn',capture)
    src=a.binding.reference('source','fixtures/object-001.bin')
    dst=a.binding.reference('destination','pilot/run1/object-001.bin')
    try:
        assert a.probe()['rclone_version']=='1.75.1'
        assert a.object_metadata(src)['existing']=='original'
        code,_=a.copy_with_tags(src,dst,tags)
        assert code==0, (errors,calls)
        assert state['payload']==payload
        assert state['multipart']==multipart
        assert state['metadata'][TAG_METADATA_KEY]==encode_tags(tags)
        assert state['metadata']['existing']=='original'
        assert state['native']==native
        assert decode_tags(a.object_metadata(dst)[TAG_METADATA_KEY])==tags
        assert a.digest(dst)==(len(payload),hashlib.sha256(payload).hexdigest())
        assert 'RCLONE_METADATA_SET' not in a.env
    finally:
        a.cancel(); server.shutdown(); server.server_close(); thread.join()
