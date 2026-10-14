"""Read tags using the provisioned reader identity in a killable subprocess."""
import json
import re
import sys

from .tags import normalize_tags


def read_tags(client, bucket, key, owner):
    result = client.get_object_tagging(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
    # Missing TagSet is not equivalent to a successful empty tag set.
    return normalize_tags(result['TagSet'])


def main():
    try:
        import boto3
        from botocore.config import Config
        region, bucket, key, owner = sys.argv[1:]
        from .cloud import key_ok
        key_ok(key)
        if (not re.fullmatch(r'(us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+', region)
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket)
                or not re.fullmatch(r'\d{12}', owner)):
            raise ValueError()
        session = boto3.Session(profile_name='migration-reader', region_name=region)
        client = session.client('s3', endpoint_url=f'https://s3.{region}.amazonaws.com',
            config=Config(connect_timeout=10, read_timeout=15, proxies={},
                          retries={'total_max_attempts': 1},
                          s3={'addressing_style': 'path'}, ignore_configured_endpoint_urls=True))
        tags = read_tags(client, bucket, key, owner)
        print(json.dumps(tags, ensure_ascii=True, sort_keys=True))
    except Exception:
        # Parent records a fixed failure. Never expose SDK responses or credentials.
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
