import copy
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_finite_amp_path_exact_and_overflow_retry_has_finite_gradients(device):
    torch = pytest.importorskip('torch')
    if device == 'cuda' and not torch.cuda.is_available():
        pytest.skip('CUDA unavailable')
    from mdva.training.models import UNet
    from mdva.training.ar_precision import guard
    torch.set_num_threads(2)
    torch.manual_seed(17)
    baseline = UNet(1).to(device)
    model = guard(copy.deepcopy(baseline))
    image = torch.rand(2, 1, 16, 16, device=device)
    with torch.autocast(device, dtype=torch.float16):
        expected, actual = baseline(image), model(image)
    assert torch.equal(expected, actual)
    assert model.precision_counters['fp32_retries'] == 0
    assert model.state_dict().keys() == baseline.state_dict().keys()
    events = []
    model = UNet(1).to(device)
    with torch.no_grad():
        for p in model.parameters():
            p.zero_()
        model.out.bias.fill_(70000.)
    model = guard(model, events.append)
    with torch.autocast(device, dtype=torch.float16):
        result = model(image)
    assert result.dtype == torch.float32 and bool(torch.isfinite(result).all())
    assert events[0]['retry'] == 1 and events[0]['training']
    result.mean().backward()
    assert all(bool(torch.isfinite(p.grad).all()) for p in model.parameters() if p.grad is not None)


def test_rejects_stateful_graph_and_bad_parameters_instead_of_clamping():
    torch = pytest.importorskip('torch')
    from mdva.training.models import UNet
    from mdva.training.ar_precision import guard
    with pytest.raises(ValueError):
        guard(torch.nn.Sequential(torch.nn.Conv2d(1, 1, 1), torch.nn.BatchNorm2d(1)))
    model = guard(UNet(1))
    with torch.no_grad():
        model.out.bias.fill_(float('nan'))
    with pytest.raises(RuntimeError, match='参数非有限'):
        model(torch.zeros(1, 1, 16, 16))


def test_failed_fp32_and_excess_retries_remain_hard_stops():
    torch = pytest.importorskip('torch')
    from mdva.training.models import UNet
    from mdva.training.ar_precision import guard
    model = UNet(1)
    model.forward = lambda x: x * 3e38 * 3
    guard(model)
    with pytest.raises(RuntimeError, match='FP32重试仍非有限'):
        model(torch.ones(1, 1, 16, 16))
    model = UNet(1)
    with torch.no_grad():
        model.out.bias.fill_(70000.)
    guard(model)
    model.precision_counters['consecutive_retries'] = 128
    with torch.autocast('cpu', dtype=torch.float16), pytest.raises(RuntimeError, match='128'):
        model(torch.ones(1, 1, 16, 16))


def test_finite_forward_resets_consecutive_budget_without_hiding_total_retries():
    torch = pytest.importorskip('torch')
    from mdva.training.models import UNet
    from mdva.training.ar_precision import guard
    model = guard(UNet(1))
    model.precision_counters.update(consecutive_retries=128,fp32_retries=128)
    model(torch.zeros(1,1,16,16))
    assert model.precision_counters['consecutive_retries'] == 0
    with torch.no_grad():
        model.out.bias.fill_(70000.)
    with torch.autocast('cpu',dtype=torch.float16):
        result = model(torch.zeros(1,1,16,16))
    assert bool(torch.isfinite(result).all())
    assert model.precision_counters['fp32_retries'] == 129
    assert model.precision_counters['consecutive_retries'] == 1


def test_guard_route_is_allowlisted_hashed_and_inference_uses_same_adapter(tmp_path):
    from mdva.training.run_locations import location, model_and_identity
    from mdva.training.runtime import digest
    import mdva.training.run_locations as resolver
    base = Path(resolver.__file__).parent
    names = ('fast_data.py', 'ar_precision.py', 'ar_guard_train.py')
    registry = {'runs': {'AR-A1-s2026': {'relative': 'revisions/io-ar-guard-v1',
                                       'entrypoint': 'mdva.training.ar_guard_train'}}}
    gate = {'enabled': True, 'runs': {'AR-A1-s2026': {}}, 'test_unlock': False,
            'code': {n: digest(base/n) for n in names}, 'acceptance_passed': True}
    (tmp_path/'run-revisions.json').write_text(json.dumps(registry))
    (tmp_path/'ar-precision-revision.json').write_text(json.dumps(gate))
    assert location(tmp_path, 'AR-A1-s2026')[1] == 'mdva.training.ar_guard_train'
    pytest.importorskip('torch')
    assert model_and_identity({'execution_revision': 'io-ar-guard-v1'})[0].__module__ == 'mdva.training.ar_precision'
    gate['code']['ar_precision.py'] = 'bad'
    (tmp_path/'ar-precision-revision.json').write_text(json.dumps(gate))
    with pytest.raises(ValueError):
        location(tmp_path, 'AR-A1-s2026')
    registry['runs']['CH-C2-s42'] = registry['runs']['AR-A1-s2026']
    (tmp_path/'run-revisions.json').write_text(json.dumps(registry))
    with pytest.raises(ValueError):
        location(tmp_path, 'CH-C2-s42')


def test_revision_resume_preserves_optimizer_rng_and_history(tmp_path, monkeypatch):
    torch = pytest.importorskip('torch')
    from mdva.training import reviewed_train as engine, ar_guard_train as trainer
    from mdva.training.fast_benchmark import equal
    import numpy as np
    data = tmp_path/'data'
    raw = tmp_path/'raw'
    data.mkdir(); raw.mkdir()
    (data/'sources.json').write_text(json.dumps({'test':str(raw)}))
    for name in ('manifest-hashes.json', 'segmentation-ready.jsonl'):
        (data/name).write_text('{}')
    np.save(raw/'mask.npy', np.ones((16,16), dtype='uint8'))
    class FakeData:
        def __init__(self, *args, **kwargs):
            self.rows = [{'uri':str(i), 'mask_uri':'mask', 'split':'validation',
                          'time_utc':'2020-01-01', 'instrument':'test'} for i in range(3)]
        def __len__(self): return len(self.rows)
        def source(self, uri): return raw/'mask.npy'
        def __getitem__(self, index):
            return {'image':torch.ones(1,256,256)*.5, 'semantic_mask':torch.ones(1,256,256)}
    monkeypatch.setattr(trainer, 'CachedDataset', FakeData)
    monkeypatch.setattr(trainer, 'architecture', lambda cfg, on_retry=None: (torch.nn.Conv2d(1,1,1), {'test':True}))
    config = {'id':'AR-A1-s2026', 'category':'AR', 'model':'unet', 'width':16, 'size':256,
              'loss':'bce_dice', 'seed':2026, 'execution_revision':'io-ar-guard-v1',
              'cache_directory':str(tmp_path/'cache'), 'max_epochs':3, 'min_epochs':3, 'patience':12}
    before = (engine.architecture, engine.code_identity, engine.loader_for, engine.SegmentationDataset)
    trainer.train(config, data, tmp_path/'continuous', device='cpu', diagnostic_limit=3)
    trainer.train(config, data, tmp_path/'resume', device='cpu', diagnostic_limit=3, stop_after=1)
    trainer.train(config, data, tmp_path/'resume', device='cpu', diagnostic_limit=3, resume=True)
    a,b = [engine.load_checkpoint(tmp_path/p/'last.pt') for p in ('continuous','resume')]
    for key in ('model','optimizer','scheduler','scaler','rng','identity'):
        assert equal(a[key],b[key])
    for x,y in zip(a['history'],b['history']):
        assert x['train_loss'] == y['train_loss'] and x['validation'] == y['validation']
    assert before == (engine.architecture, engine.code_identity, engine.loader_for, engine.SegmentationDataset)
