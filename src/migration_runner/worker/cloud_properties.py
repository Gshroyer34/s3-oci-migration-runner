"""Bounded read-only SDK helper, run under the existing killable worker boundary."""
import json
import re
import sys
from .properties import HEAD_FIELDS, REVISION, json_value


def read_source(client, bucket, key, owner):
    args = dict(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
    head = client.head_object(**args)
    acl_args = dict(args)
    if head.get('VersionId'):
        acl_args['VersionId'] = head['VersionId']
    try:
        response = client.get_object_acl(**acl_args)
        if (not isinstance(response.get('Owner'), dict) or not response['Owner'].get('ID')
                or not isinstance(response.get('Grants'), list)):
            raise ValueError('Incomplete ACL response')
        acl = {'status': 'captured', 'Owner': response['Owner'], 'Grants': response['Grants']}
    except Exception as error:
        # An explicit lack of permission is visible, never represented as an empty ACL.
        code = getattr(error, 'response', {}).get('Error', {}).get('Code')
        if code not in ('AccessDenied', 'AllAccessDisabled', 'NotImplemented'):
            raise
        acl = {'status': 'not_captured', 'reason': 'permission_or_api_unavailable'}
    return json_value({'revision': REVISION, 'head': {k: head[k] for k in HEAD_FIELDS if k in head},
                       'acl': acl, 'checksum_discovery': 'HEAD default; additional checksum retrieval not requested'})


def read_destination(client, namespace, bucket, key):
    headers = client.head_object(namespace, bucket, key).headers
    allowed = {'content-type', 'content-length', 'content-encoding', 'content-disposition',
               'content-language', 'cache-control', 'etag', 'last-modified', 'version-id',
               'storage-tier', 'opc-content-md5'}
    return {k.lower(): v for k, v in headers.items() if k.lower() in allowed or k.lower().startswith('opc-meta-')}


def main():
    try:
        from .cloud import key_ok
        side, region, bucket, key, identity = sys.argv[1:]
        key_ok(key)
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ValueError()
        if side == 'source':
            import boto3
            from botocore.config import Config
            if not re.fullmatch(r'(us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+', region) or not re.fullmatch(r'\d{12}', identity):
                raise ValueError()
            client = boto3.Session(profile_name='migration-reader', region_name=region).client('s3',
                endpoint_url=f'https://s3.{region}.amazonaws.com', config=Config(
                    connect_timeout=10, read_timeout=15, proxies={}, retries={'total_max_attempts': 1},
                    s3={'addressing_style': 'path'}, ignore_configured_endpoint_urls=True))
            value = read_source(client, bucket, key, identity)
        elif side == 'destination':
            import oci
            if not re.fullmatch(r'[a-z]{2}-[a-z]+-\d+', region) or not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
                raise ValueError()
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            client = oci.object_storage.ObjectStorageClient({'region': region}, signer=signer,
                timeout=(10, 15), retry_strategy=oci.retry.NoneRetryStrategy())
            value = read_destination(client, identity, bucket, key)
        else:
            raise ValueError()
        print(json.dumps(value, ensure_ascii=True, sort_keys=True))
    except Exception:
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
