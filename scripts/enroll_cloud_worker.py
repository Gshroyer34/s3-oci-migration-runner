"""Create a private pilot token or enroll its hash once through coordinator mTLS."""
import argparse,json,os,secrets
from pathlib import Path


def create_token(path):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='ascii') as stream: stream.write(secrets.token_urlsafe(48)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--create-token',type=Path)
    group.add_argument('--enroll',action='store_true')
    args=parser.parse_args(); engine=None
    try:
        if args.create_token:
            create_token(args.create_token)
            print(json.dumps({'private_token_created':True,'cloud_contacted':False})); return 0
        from sqlalchemy import text
        from migration_runner.config import Settings
        from migration_runner.ledger.database import make_engine
        from migration_runner.security import digest
        settings=Settings()
        if settings.environment!='cloud-cli': raise ValueError('Coordinator certificate mode required')
        token=settings.worker_token.get_secret_value()
        engine=make_engine(settings)
        with engine.begin() as connection:
            # Refuse to replace an existing enrollment; no accidental global token rotation.
            connection.execute(text('INSERT INTO runtime_security (id, worker_token_hash) VALUES (1, :token_hash)'),{'token_hash':digest(token)})
        print(json.dumps({'enrolled':True})); return 0
    except Exception:
        print(json.dumps({'error':'Enrollment rejected; check private paths, coordinator TLS, or existing enrollment'})); return 1
    finally:
        if engine is not None: engine.dispose()


if __name__=='__main__': raise SystemExit(main())
