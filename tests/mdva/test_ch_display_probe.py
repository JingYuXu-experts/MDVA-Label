from pathlib import Path
import json

import pytest

from mdva.training.ch_display_probe import validate_preparation
from mdva.training.runtime import digest


def fixture(tmp_path):
    data=tmp_path/'data';data.mkdir();(data/'manifest-hashes.json').write_text('{}')
    prep=tmp_path/'prep';prep.mkdir();(prep/'images.jsonl').write_text('')
    from mdva.training import ch_display_audit,spatial_audit
    protocol={'anchor_fit_split':'train','category':'CH','threshold':.5,
        'frozen_manifest_sha256':digest(data/'manifest-hashes.json'),
        'input_rows_sha256':digest(prep/'images.jsonl'),
        'code':{Path(m.__file__).name:digest(m.__file__) for m in (ch_display_audit,spatial_audit)}}
    (prep/'protocol.json').write_text(json.dumps(protocol))
    return prep,data,protocol


@pytest.mark.parametrize('change', ['validation_anchor','threshold','code_missing','changed_input','changed_manifest'])
def test_protocol_changes_rejected_before_any_model_loading(tmp_path,change):
    prep,data,protocol=fixture(tmp_path)
    assert validate_preparation(prep,data)==protocol
    if change=='validation_anchor':protocol['anchor_fit_split']='validation'
    elif change=='threshold':protocol['threshold']=.4
    elif change=='code_missing':protocol['code']={}
    elif change=='changed_input':(prep/'images.jsonl').write_text('changed')
    else:(data/'manifest-hashes.json').write_text('changed')
    (prep/'protocol.json').write_text(json.dumps(protocol))
    with pytest.raises(ValueError):validate_preparation(prep,data)
