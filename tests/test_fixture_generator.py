import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('fixture_generator',Path(__file__).parents[1]/'scripts/generate_pilot_fixture.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def test_small_fixture_independent_hashes_and_no_overwrite(tmp_path):
    output=tmp_path/'fixture'
    result=module.generate(output,3,1024*1024+3)
    manifest=json.loads((output/'reference.json').read_text())
    assert result['total_bytes']==3*(1024*1024+3) and not result['cloud_contacted']
    hashes=[]
    for record in manifest['objects']:
        payload=(output/record['source_key'].removeprefix('fixtures/')).read_bytes()
        assert len(payload)==record['size'] and hashlib.sha256(payload).hexdigest()==record['sha256']
        hashes.append(record['sha256'])
    assert len(set(hashes))==3
    with pytest.raises(FileExistsError): module.generate(output,1,1)
    with pytest.raises(ValueError): module.generate(tmp_path/'too-large',101,100_000_000)
    assert not (tmp_path/'too-large').exists()
