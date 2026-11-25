import json

import pytest

from mdva.training.runtime import atomic_json
from mdva.training.run_locations import location, run_folder


def test_numerical_revision_preserves_completed_parent_and_routes_only_ch(tmp_path):
    from mdva.web.training import detail, summary, artifact
    output = tmp_path/'out'; parent = output/'training-20261112-v2'; parent.mkdir(parents=True)
    configs = [{'id':'AR-A1-s17'}, {'id':'CH-C2-s17'}]
    atomic_json(parent/'protocol.json', {'recognition':configs,'resources':{}})
    atomic_json(parent/'queue.json', {'order':[r['id'] for r in configs]})
    atomic_json(parent/'queue-state.json', {'active':'CH-C2-s17'})
    atomic_json(parent/'runs/AR-A1-s17/status.json', {'id':'AR-A1-s17','state':'completed','completed_epoch':48})
    old = parent/'runs/CH-C2-s17/status.json'
    atomic_json(old, {'id':'CH-C2-s17','state':'failed','completed_epoch':1})
    evidence = old.read_bytes()
    scope = parent/'revisions/ch-fp32-v1'
    atomic_json(scope/'configs/CH-C2-s17.json', {'id':'CH-C2-s17','cnn193_compute':'fp32'})
    atomic_json(scope/'runs/CH-C2-s17/status.json', {'id':'CH-C2-s17','state':'running','completed_epoch':0})
    (scope/'runs/CH-C2-s17/console.log').write_text('FP32修复训练', encoding='utf-8')
    atomic_json(parent/'run-revisions.json', {'runs':{'CH-C2-s17':{'relative':'revisions/ch-fp32-v1','entrypoint':'mdva.training.cnn193_fp32'}}})
    value = summary(tmp_path, output=output)
    assert value['completed']==1
    assert value['runs'][1]['state']=='running'
    assert detail(tmp_path,'CH-C2-s17',output=output)['config']['cnn193_compute']=='fp32'
    assert 'FP32修复' in artifact(tmp_path,'current-console',output=output)['text']
    assert old.read_bytes()==evidence
    assert run_folder(parent,'AR-A1-s17')==parent/'runs/AR-A1-s17'
    atomic_json(parent/'run-revisions.json', {'runs':{'CH-C2-s17':{'relative':'../../private','entrypoint':'arbitrary'}}})
    with pytest.raises(ValueError):location(parent,'CH-C2-s17')


def test_cnn_adapter_blocks_outer_amp_overflow_without_changing_parameter_layout(monkeypatch):
    torch = pytest.importorskip('torch')
    from mdva.training import stable_models
    from mdva.training.cnn193_fp32 import architecture
    def base(config):
        model = torch.nn.Conv2d(1,1,1,bias=False)
        with torch.no_grad(): model.weight.fill_(2)
        return model, {'test':True}
    monkeypatch.setattr(stable_models,'architecture',base)
    model,meta = architecture({'model':'cnn193','cnn193_compute':'fp32'})
    with torch.autocast('cpu',dtype=torch.float16):
        result = model(torch.full((1,1,4,4),49088.))
    assert torch.isfinite(result).all() and result.dtype==torch.float32
    result.mean().backward()
    assert torch.isfinite(model.weight.grad).all()
    assert list(model.state_dict())==['weight']
    assert meta['numerical_adaptation']['equations_and_parameter_shapes_changed'] is False
