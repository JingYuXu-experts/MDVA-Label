import json
import pytest
from mdva.training.fl_rate_study import prepare


def test_rate_control_does_not_start_from_incomplete_or_changed_evidence(tmp_path):
    parent=tmp_path/'parent';parent.mkdir();dataset=tmp_path/'dataset';dataset.mkdir()
    (dataset/'sources.json').write_text('{}')
    (parent/'protocol.json').write_text(json.dumps({'dataset':str(dataset)}))
    (parent/'summary.json').write_text(json.dumps({'state':'running'}))
    with pytest.raises(ValueError,match='先结束'):prepare(parent,tmp_path/'out')
    (parent/'summary.json').write_text(json.dumps({'state':'completed','protocol_sha256':'tampered'}))
    with pytest.raises(ValueError,match='协议或代码改变'):prepare(parent,tmp_path/'out')
    assert not (tmp_path/'out').exists()
