"""Build-time download of the pinned public AWS helper; no AWS credentials."""
import hashlib
from pathlib import Path
import urllib.request

url='https://rolesanywhere.amazonaws.com/releases/1.8.5/X86_64/Linux/Amzn2023/aws_signing_helper'
target=Path('/usr/local/bin/aws_signing_helper')
with urllib.request.urlopen(url,timeout=120) as response:
    data=response.read(100_000_001)
if len(data)>100_000_000 or hashlib.sha256(data).hexdigest()!='beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d':
    raise SystemExit('AWS helper checksum failed')
target.write_bytes(data); target.chmod(0o755)
