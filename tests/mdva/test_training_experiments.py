import json

import numpy as np
import pytest

from mdva.training.metrics import (binary_segmentation, forecast_scores, holm,
                                   paired_cluster_interval, segmentation_summary)
from mdva.training.protocol import experiment_matrix
from mdva.training.runtime import output_path
from mdva.training.forecast import purged_fold, sequences


def test_matrix_has_42_unique_runs_and_only_registered_interventions():
    configs = experiment_matrix()
    assert len(configs) == len({c['id'] for c in configs}) == 42
    for category, count in [('AR', 6), ('CH', 9), ('FL', 27)]:
        assert sum(c['category'] == category for c in configs) == count
    assert all(not c['augment'] for c in configs if c['category'] == 'AR')
    baseline = next(c for c in configs if c['id'] == 'FL-F2-s17')
    minus_dice = next(c for c in configs if c['id'] == 'FL-F3-s17')
    assert {k for k in baseline if baseline[k] != minus_dice[k]} == {'id', 'loss'}


def test_native_grid_metrics_do_not_reward_empty_foreground():
    empty = np.zeros((16, 16), bool)
    fp = empty.copy(); fp[4, 4] = True
    a = binary_segmentation(empty, empty)
    b = binary_segmentation(fp, empty)
    assert a['dice'] is b['dice'] is None
    summary = segmentation_summary([a, b])
    assert summary['empty_false_positive_rate'] == .5
    assert summary['dice'] is None
    target = empty.copy(); target[3:7, 5:8] = True
    perfect = binary_segmentation(target, target, boundary=True)
    assert perfect['dice'] == perfect['iou'] == perfect['boundary_f1'] == 1.
    with pytest.raises(ValueError): binary_segmentation(empty[:8], empty)


def test_forecast_uses_frozen_climate_and_reports_false_alarms():
    s = forecast_scores([0, 0, 1, 1], [.1, .8, .9, .2], .5, climate=.1)
    assert (s['tp'], s['fp'], s['fn'], s['tn']) == (1, 1, 1, 1)
    assert s['tss'] == s['hss'] == 0
    assert s['false_alarm_ratio'] == .5
    with pytest.raises(ValueError): forecast_scores([0], [float('nan')])


def test_bootstrap_pairs_clusters_and_holm_controls_comparisons():
    result = paired_cluster_interval([0, 0, 0], [1, 1, 1], ['a', 'a', 'b'], repeats=2000)
    assert result['ci95'] == [1., 1.] and result['clusters'] == 2
    assert holm([.04, .01, .03]) == pytest.approx([.06, .03, .06])


def test_output_rejects_all_readonly_source_descendants(tmp_path):
    raw = tmp_path/'raw'; raw.mkdir()
    frozen = tmp_path/'frozen'; frozen.mkdir()
    (frozen/'sources.json').write_text(json.dumps({'test': str(raw)}))
    for path in (raw, raw/'nested', frozen/'nested'):
        with pytest.raises(ValueError): output_path(path, frozen)
    assert output_path(tmp_path/'runs', frozen) == tmp_path/'runs'


def test_rolling_fold_purges_region_identity_and_boundary_windows():
    def row(t, region): return {'time_utc': t+'T00:00:00+00:00', 'noaa': region}
    rows = [row('2011-03-01', 11001), row('2011-04-01', 11002), row('2011-12-31', 11003),
            row('2012-01-02', 11004), row('2012-03-01', 11001), row('2012-04-01', 11005)]
    train, valid = purged_fold(rows, 2011, 2012, 2013)
    assert [r['noaa'] for r in train] == [11002]
    assert {r['noaa'] for r in valid} == {11001, 11005}


def test_sequences_reject_gaps_duplicates_and_cross_split_history():
    rows = []
    for day in (1, 2, 3, 5, 6):
        rows.append({'time_utc': f'2020-03-{day:02}T00:00:00+00:00', 'noaa': 12001,
                     'split': 'train', 'features': {k: day for k in ('MGB_PIL','R','MGB','Total_Unsigned_Flux','Area')},
                     'target_m_plus': int(day == 3), 'instance_id': 1})
    x, y, selected, history = sequences(rows, 3)
    assert x.shape == (1, 3, 5) and y.tolist() == [1]
    assert x[0, :, 0].tolist() == [1, 2, 3]
    assert len(history[0]) == 3
    rows.append(dict(rows[1]))
    assert sequences(rows, 3)[0].shape[0] == 0


def test_ar_object_scoring_keeps_disconnected_reference_instance_identity():
    from mdva.training.metrics import object_scores
    mask = np.zeros((16, 16), int)
    mask[2:5, 2:5] = mask[10:13, 10:13] = 3
    result = object_scores(mask > 0, mask)
    assert result['reference_objects'] == 1
    assert result['predicted_components'] == 2 and result['split_candidates'] == 1


def test_epoch_restore_replays_rng_optimizer_and_partial_accumulation(tmp_path, monkeypatch):
    torch = pytest.importorskip('torch', reason='训练器集成测试需模型Conda环境')
    from mdva.training import train as trainer
    raw = tmp_path/'raw'; raw.mkdir()
    frozen = tmp_path/'frozen'; frozen.mkdir()
    for name in ('manifest-hashes.json', 'segmentation-ready.jsonl'):
        (frozen/name).write_text('{}')
    (frozen/'sources.json').write_text(json.dumps({'test': str(raw)}))
    mask = np.zeros((16, 16), int); mask[4:12, 4:12] = 1
    np.save(raw/'mask.npy', mask)
    class FakeDataset:
        def __init__(self, directory, category, split, size):
            self.rows = [{'uri': str(i), 'mask_uri': 'mask', 'time_utc': '2020-01-01T00:00:00+00:00',
                          'instrument': 'test', 'split': split} for i in range(3)]
        def __len__(self): return len(self.rows)
        def source(self, uri): return raw/'mask.npy'
        def __getitem__(self, i):
            y = torch.zeros(1, 256, 256); y[:, 64:192, 64:192] = 1
            return {'image': y*.8+.1, 'semantic_mask': y}
    monkeypatch.setattr(trainer, 'SegmentationDataset', FakeDataset)
    monkeypatch.setattr(trainer, 'architecture', lambda c: (torch.nn.Sequential(torch.nn.Conv2d(1, 1, 1), torch.nn.Dropout(.1)), {'test': True}))
    config = {'id': 'test-resume', 'model': 'unet', 'width': 16, 'size': 256,
              'category': 'FL', 'loss': 'bce_dice', 'seed': 17, 'max_epochs': 3,
              'min_epochs': 3, 'effective_batch': 8}
    trainer.train(config, frozen, tmp_path/'direct', device='cpu', diagnostic_limit=3)
    trainer.train(config, frozen, tmp_path/'resumed', device='cpu', diagnostic_limit=3, stop_after=1)
    trainer.train(config, frozen, tmp_path/'resumed', device='cpu', diagnostic_limit=3, resume=True)
    a, b = [trainer.load_checkpoint(tmp_path/p/'last.pt') for p in ('direct', 'resumed')]
    assert all(torch.equal(a['model'][k], b['model'][k]) for k in a['model'])
    assert a['optimizer']['param_groups'] == b['optimizer']['param_groups']
    assert torch.equal(a['rng']['torch'], b['rng']['torch'])
    for name in ('train_loss', 'validation'):
        assert [r[name] for r in a['history']] == [r[name] for r in b['history']]
    changed = dict(config, loss='bce')
    with pytest.raises(ValueError, match='版本不同'):
        trainer.train(changed, frozen, tmp_path/'resumed', device='cpu', diagnostic_limit=3, resume=True)


@pytest.mark.parametrize('name', ['SCA_ConvBlock', 'CSA_ConvBlock'])
def test_attention_adapter_prevents_finite_half_values_overflowing_spatial_sum(name):
    torch = pytest.importorskip('torch')
    from mdva.training.stable_models import stabilize_attention
    class LargeProjection(torch.nn.Module):
        def forward(self, x): return torch.full_like(x, 128)
    def initialize(self):
        torch.nn.Module.__init__(self)
        self.fq, self.fk, self.fv = LargeProjection(), LargeProjection(), LargeProjection()
        self.bn, self.relu = torch.nn.Identity(), torch.nn.ReLU()
    block = type(name, (torch.nn.Module,), {'__init__': initialize})()
    assert stabilize_attention(block) == 1
    result = block(torch.ones(1, 2, 8, 8, dtype=torch.float16))
    assert torch.isfinite(result).all()
    assert torch.allclose(result, torch.full_like(result, 65))


def test_nonfinite_validation_is_rejected_before_scoring_as_background(tmp_path):
    torch = pytest.importorskip('torch')
    from mdva.training.train import evaluate
    class BadModel(torch.nn.Module):
        def forward(self, x): return x*float('nan')
    class Data:
        rows = [{'uri': 'known-image'}]
        def __getitem__(self, i): return {'image': torch.zeros(1, 16, 16), 'semantic_mask': torch.zeros(1, 16, 16)}
    with pytest.raises(RuntimeError, match='非有限'):
        evaluate(BadModel(), Data(), {'loss': 'bce'}, device='cpu')
