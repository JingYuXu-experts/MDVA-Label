import json
from pathlib import Path

import numpy as np
import pytest

from mdva.training import diagnostic_support as support
from mdva.training.diagnose import ensure_stopped


def row(uri, station='BBSO', split='train', area=20, category='FL'):
    return {'uri': uri, 'category': category, 'instrument': station, 'split': split,
            'mask_statistics': {'pixels': {'0': 1000-area, '1': area}}}


def test_sample_selection_is_fixed_common_station_balanced_and_preserves_empty_reference():
    rows = [row(f'@s/{station}-{i}', station=station, area=i+1)
            for station in ('BBSO', 'KSO', 'HSOS') for i in range(90)]
    rows += [row('@s/empty', area=0), row('@s/val', split='validation')]
    a, b = support.select_samples(rows), support.select_samples(list(reversed(rows)))
    assert sorted(x['uri'] for x in a) == sorted(x['uri'] for x in b)
    for station in ('BBSO', 'KSO', 'HSOS'):
        assert sum(r['instrument'] == station and r['diagnostic_subset'] == 'train_fit_sample' for r in a) == 30
    assert sum(r['diagnostic_subset'] == 'train_empty_reference' for r in a) == 1
    assert len({r['sample_id'] for r in a}) == len(a)


def test_forbidden_split_metadata_never_enters_allowed_pixels(tmp_path, monkeypatch):
    from mdva.training import datasets
    rows = [{**row('@s/'+s, split=s), 'eligible': True} for s in ('train', 'validation', 'calibration', 'test')]
    monkeypatch.setattr(datasets, 'read_frozen', lambda *a: rows)
    assert {r['split'] for r in support.allowed_rows(tmp_path)} == {'train', 'validation'}


def test_known_raster_metrics_empty_foreground_and_diagonal_connectivity():
    mask = np.zeros((16, 16), np.uint8); mask[2:6, 3:7] = 1
    values = support.score(mask.astype(float), mask, 'FL')
    assert values['dice'] == values['iou'] == values['boundary_f1'] == 1
    assert values['objects']['matched'] == 1
    assert len(values['thresholds']) == 17
    assert values['error_types'] == ['no_major_flag']
    missed = support.score(np.zeros_like(mask, float), mask, 'FL')
    assert missed['dice'] == 0 and 'empty_prediction' in missed['error_types']
    empty = support.score(np.zeros_like(mask, float), np.zeros_like(mask), 'CH')
    assert empty['dice'] is None and not empty['false_positive_image']
    diagonal = np.eye(3, dtype=np.uint8)
    assert support.score(diagonal.astype(float), diagonal, 'CH')['objects']['reference_regions'] == 1


def test_pause_and_exclusive_worker_checks(tmp_path, monkeypatch):
    from mdva.training import continuation
    with pytest.raises(ValueError, match='暂停'):
        ensure_stopped(tmp_path)
    (tmp_path/'PAUSE').touch()
    monkeypatch.setattr(continuation, 'lock_alive', lambda *a: True)
    with pytest.raises(ValueError, match='运行'):
        ensure_stopped(tmp_path)
    monkeypatch.setattr(continuation, 'lock_alive', lambda *a: False)
    ensure_stopped(tmp_path)


def test_atomic_file_retries_and_preserves_old_on_permanent_failure(tmp_path, monkeypatch):
    path = tmp_path/'state.json'; support.write(path, {'epoch': 1})
    original = support.os.replace
    calls = []
    def replace(a, b):
        calls.append(a)
        if len(calls) < 3:
            raise PermissionError('held')
        return original(a, b)
    monkeypatch.setattr(support.os, 'replace', replace)
    monkeypatch.setattr(support.time, 'sleep', lambda t: None)
    support.write(path, {'epoch': 2})
    assert support.read(path)['epoch'] == 2 and len(calls) == 3
    monkeypatch.setattr(support.os, 'replace', lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        support.write(path, {'epoch': 3})
    assert support.read(path)['epoch'] == 2 and not list(tmp_path.glob('*.tmp'))


def test_comparisons_average_seeds_before_resampling_and_skip_partial():
    models, rows = [], []
    for group, value in [('FL-F0', .5), ('FL-F2', .6)]:
        for seed in [17, 42, 2026]:
            name = f'{group}-s{seed}'
            models.append({'id': name, 'state': 'completed', 'category': 'FL', 'reproduction_passed': True})
            for n in range(6):
                rows.append({'run_id': name, 'split': 'validation', 'sample_id': str(n), 'dice': value,
                             'time_utc': f'2020-0{n+1}-01T00:00:00+00:00'})
    result = support.comparisons(models, rows)
    assert len(result) == 1 and result[0]['images'] == 6
    assert result[0]['difference_b_minus_a'] == pytest.approx(.1)
    models[-1]['partial_training'] = True
    assert support.comparisons(models, rows) == []


def report_fixture(tmp_path):
    folder = tmp_path/'model-diagnostics/20261115-01'
    support.write(folder/'identity.json', {'schema': 1})
    support.write(folder/'status.json', {'state': 'running', 'phase': 'D2'})
    support.write(folder/'report.json', {'models': []})
    rows = []
    for model in ['FL-F0-s17', 'FL-F2-s17']:
        for i in range(30):
            sid = f'{i:024x}'
            asset_id = sid+'/original.png'
            path = folder/'assets'/asset_id; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'png fixture')
            rows.append({'sample_id': sid, 'run_id': model, 'category': 'FL', 'diagnostic_subset': 'validation',
                         'instrument': 'BBSO' if i % 2 else 'KSO', 'area_bin': 'small' if i % 3 else 'large',
                         'dice': i/30, 'error_types': ['undersegmentation'] if i < 15 else ['no_major_flag'],
                         'assets': {'original': asset_id}, 'thresholds': [1, 2, 3]})
    support.write(folder/'case-index.json', rows)
    return folder


def test_read_only_api_pagination_filters_same_image_and_path_validation(tmp_path):
    from mdva.web import diagnostics
    from mdva.web.deps import Workspaces
    from mdva.web.routes import call
    folder = report_fixture(tmp_path)
    ws = Workspaces(tmp_path/'data', out_dir=tmp_path)
    assert call(ws, 'GET', '/api/training/diagnostics').json_body['available']
    result = call(ws, 'GET', '/api/training/diagnostics/cases', {'category': 'FL'})
    assert result.status == 200 and len(result.json_body['items']) == 24
    assert result.json_body['total'] == 30
    assert 'thresholds' not in result.json_body['items'][0]
    item = result.json_body['items'][0]
    compare = diagnostics.cases(tmp_path, run_id='FL-F2-s17', sample_id=item['sample_id'])
    assert compare['total'] == 1 and compare['items'][0]['sample_id'] == item['sample_id']
    second = diagnostics.cases(tmp_path, page=2)
    assert len(second['items']) == 6
    assert not set(r['sample_id'] for r in second['items']) & set(r['sample_id'] for r in result.json_body['items'])
    assert diagnostics.cases(tmp_path, station='KSO', error_type='undersegmentation')['total'] == 8
    for params in ({'subset': 'test'}, {'subset': 'calibration'}, {'report': '../private'}, {'page': '0'}, {'run_id': '../private'}):
        assert call(ws, 'GET', '/api/training/diagnostics/cases', params).status == 400
    for value in ('../../secret', 'C:/Windows/win.ini', 'report.json/../../secret'):
        assert call(ws, 'GET', '/api/training/diagnostics/assets', {'id': value}).status == 400
    content = call(ws, 'GET', '/api/training/diagnostics/assets', {'report': folder.name, 'id': item['assets']['original']})
    assert content.status == 200 and content.data == b'png fixture'
    assert call(ws, 'POST', '/api/training/diagnostics').status == 405
    assert not ws._hubs


def test_no_report_is_honest_and_invalid_paths_are_rejected(tmp_path):
    from mdva.web import diagnostics
    assert diagnostics.summary(tmp_path)['available'] is False
    report_fixture(tmp_path)
    with pytest.raises(ValueError):
        diagnostics.cases(tmp_path, identifier='..')
    with pytest.raises(FileNotFoundError):
        diagnostics.asset(tmp_path, '20261115-01', 'f'*24+'/original.png')


def test_probability_cache_version_checksum_and_broken_asset(tmp_path):
    from PIL import Image
    from mdva.training.runtime import digest
    probability = tmp_path/'p.npy'; np.save(probability, np.ones((4, 4), dtype=np.float32))
    assets = tmp_path/'assets'; assets.mkdir()
    Image.fromarray(np.zeros((4, 4), np.uint8)).save(assets/'image.png')
    previous = {'checkpoint_sha256': 'weights-v1', 'probability_sha256': digest(probability), 'assets': {'original': 'image.png'}}
    assert support.cache_ready(previous, 'weights-v1', probability, assets)
    with pytest.raises(ValueError, match='版本'):
        support.cache_ready(previous, 'weights-v2', probability, assets)
    probability.write_bytes(b'corrupt')
    assert not support.cache_ready(previous, 'weights-v1', probability, assets)
    np.save(probability, np.ones((4, 4), dtype=np.float32))
    (assets/'image.png').write_bytes(b'invalid png')
    assert not support.cache_ready(previous, 'weights-v1', probability, assets)


def test_broken_preview_assets_are_regenerated_without_changing_reference_pixels(tmp_path):
    from PIL import Image
    from mdva.training.diagnose import build_assets
    row = {'sample_id': 'a'*24}
    base = tmp_path/'assets'/row['sample_id']; base.mkdir(parents=True)
    for name in ('original.png', 'reference.png', 'input-8.png'):
        (base/name).write_bytes(b'broken')
    mask = np.zeros((8, 8), np.uint8); mask[2:4, 2:4] = 1
    old = mask.copy()
    gray = np.ones((8, 8), np.uint8)*70
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    assets = build_assets(tmp_path, row, rgb, gray, mask, mask.astype(float), 'FL-F0-s17')
    for name in assets.values():
        with Image.open(tmp_path/'assets'/name) as im:
            im.verify()
    assert np.array_equal(mask, old)


def test_cached_audit_revalidates_raw_hash_even_if_size_and_mtime_match(tmp_path, monkeypatch):
    import os
    from mdva.training import datasets
    from mdva.training.diagnose import audit
    from mdva.training.runtime import digest
    dataset = tmp_path/'dataset'; dataset.mkdir()
    sources = tmp_path/'sources'; sources.mkdir()
    image, mask = sources/'a.jpg', sources/'a.npy'
    image.write_bytes(b'original'); mask.write_bytes(b'reference')
    fingerprints = []
    for f in (image, mask):
        fingerprints.append({'uri': '@s/'+f.name, 'bytes': f.stat().st_size, 'mtime_ns': f.stat().st_mtime_ns, 'sha256': digest(f)})
    support.write(dataset/'sources.json', {'s': str(sources)})
    monkeypatch.setattr(datasets, 'read_frozen', lambda *a: fingerprints)
    out = tmp_path/'output'; support.write(out/'audit.json', {'cached': True})
    rows = [{'uri': '@s/a.jpg', 'mask_uri': '@s/a.npy', 'split': 'validation'}]
    assert audit(dataset, out, rows)['cached']
    stamp = image.stat().st_mtime_ns
    image.write_bytes(b'modified'); os.utime(image, ns=(stamp, stamp))
    with pytest.raises(ValueError, match='变化'):
        audit(dataset, out, rows)


def test_translation_probe_zero_pads_and_never_changes_reference():
    from mdva.training.diagnostic_review import shifted
    a = np.zeros((4, 4), bool); a[0, 0] = True
    b = shifted(a, 1, 2)
    assert b[1, 2] and b.sum() == 1 and a[0, 0]
    assert not shifted(a, -1, 0).any()


def test_diagnostic_hold_changes_next_step_without_rewriting_failed_run(tmp_path):
    from mdva.web.training import summary
    folder = tmp_path/'out/training-test'
    support.write(folder/'protocol.json', {'recognition': [{'id': 'AR-A0-s17'}], 'resources': {}})
    support.write(folder/'runs/AR-A0-s17/status.json', {'id': 'AR-A0-s17', 'state': 'failed', 'completed_epoch': 26, 'error': 'WinError 5'})
    (folder/'PAUSE').touch()
    support.write(folder/'diagnostic-hold.json', {'report_id': '20261115-02'})
    result = summary(tmp_path)
    assert result['completed'] == 0 and result['runs'][0]['state'] == 'failed'
    assert not result['progress']['automatic_continuation']
    assert '诊断待确认' in result['progress']['headline']
    assert result['progress']['next_commands'][0]['command'] == 'python -m mdva.training status'


def test_station_uncertainty_reports_insufficient_time_blocks(tmp_path):
    from mdva.training.diagnostic_statistics import station_intervals
    support.write(tmp_path/'report.json', {'groups': [{'group': 'FL-F0'}], 'models': [
        {'id': f'FL-F0-s{s}', 'state': 'completed', 'reproduction_passed': True} for s in [17, 42, 2026]]})
    rows = [{'run_id': f'FL-F0-s{s}', 'sample_id': str(i), 'split': 'validation', 'instrument': 'KSO',
             'time_utc': f'2020-01-0{i+1}T00:00:00+00:00', 'dice': .5} for s in [17, 42, 2026] for i in range(4)]
    support.write(tmp_path/'case-index.json', rows)
    r = station_intervals(tmp_path)['station_intervals'][0]
    assert r['images'] == 4 and r['seeds'] == 3 and r['time_blocks'] == 1
    assert r['ci95'] is None


def test_partial_failed_checkpoint_never_becomes_default_completed_model(tmp_path):
    from mdva.web import diagnostics
    folder = report_fixture(tmp_path)
    rows = support.read(folder/'case-index.json')
    for r in rows:
        r['category'] = 'AR'
        r['run_id'] = 'AR-A0-s17' if r['run_id'] == 'FL-F0-s17' else 'AR-A1-s17'
    support.write(folder/'case-index.json', rows)
    support.write(folder/'report.json', {'models': [{'id': 'AR-A0-s17', 'partial_training': True},
                                                   {'id': 'AR-A1-s17', 'partial_training': False}]})
    assert diagnostics.cases(tmp_path, category='AR')['run_id'] == 'AR-A1-s17'
    assert diagnostics.cases(tmp_path, category='AR', run_id='AR-A0-s17')['total'] == 30
