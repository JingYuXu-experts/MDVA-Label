import json

import pytest

from mdva.training import fl_sampling_control as control
from mdva.training.runtime import digest


def test_half_inverse_sampling_preserves_epoch_draws_and_seed():
    class Dataset:
        rows = [{'instrument':'major'}]*16 + [{'instrument':'minor'}]*4
        def __len__(self):
            return len(self.rows)
        def __getitem__(self,index):
            return index

    first = control.loader_for(Dataset(), {'seed':2026}, 0, 2)
    second = control.loader_for(Dataset(), {'seed':2026}, 0, 2)
    assert first.sampler.weights[16] / first.sampler.weights[0] == 2
    assert first.sampler.num_samples == len(Dataset())
    assert [batch.tolist() for batch in first] == [batch.tolist() for batch in second]


def test_sampling_control_waits_for_original_budget(tmp_path):
    original = tmp_path/'control'
    (original/'run').mkdir(parents=True)
    (original/'protocol.json').write_text('{}')
    (original/'run/status.json').write_text(json.dumps({'state':'running','completed_epoch':7}))
    study = tmp_path/'study'
    study.mkdir()
    (study/'protocol.json').write_text(json.dumps({'control':str(original), 'sampling':'half_inverse',
        'control_protocol_sha256':digest(original/'protocol.json')}))
    with pytest.raises(ValueError,match='等待对照组保存'):
        control.train(study)
    assert not (study/'run').exists()
