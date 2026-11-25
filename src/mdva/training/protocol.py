"""Freeze the agreed experiment matrix and explicit scientific release gates."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from .datasets import read_frozen
from .runtime import atomic_json, digest, now, output_path

SEEDS = (17, 42, 2026)


def experiment_matrix():
    base = {'size': 512, 'width': 32, 'loss': 'bce_dice', 'augment': True,
            'station_balance': True, 'attention': 'sca4_csa1', 'model': 'flat_unet',
            'micro_batch': 2, 'effective_batch': 8, 'max_epochs': 80,
            'min_epochs': 20, 'patience': 12, 'threshold': .5,
            'precision_policy': 'AMP convolutions; Flat attention float32 highest, including reductions/softmax/BN'}
    recipes = {
        'F0': {'model': 'unet'},
        'F1': {'loss': 'bce', 'augment': False, 'station_balance': False},
        'F2': {}, 'F3': {'loss': 'bce'}, 'F4': {'augment': False},
        'F5': {'station_balance': False}, 'F6': {'attention': 'all_sca'},
        'F7': {'width': 16}, 'F8': {'width': 64},
    }
    result = []
    for name, changes in recipes.items():
        for seed in SEEDS:
            result.append({**base, **changes, 'category': 'FL', 'seed': seed, 'id': f'FL-{name}-s{seed}'})
    for name, loss in [('A0', 'bce'), ('A1', 'bce_dice')]:
        for seed in SEEDS:
            result.append({**base, 'category': 'AR', 'model': 'unet', 'loss': loss,
                           'augment': False, 'station_balance': False, 'seed': seed, 'id': f'AR-{name}-s{seed}'})
    for name, size, loss, augment in [('C0', 256, 'bce', False),
                                       ('C1', 512, 'bce', False), ('C2', 512, 'bce_dice', True)]:
        for seed in SEEDS:
            result.append({**base, 'category': 'CH', 'model': 'cnn193', 'width': 24,
                           'size': size, 'loss': loss, 'augment': augment, 'station_balance': False,
                           'seed': seed, 'id': f'CH-{name}-s{seed}'})
    return result


def audit(dataset):
    events = read_frozen(dataset, 'events.jsonl')
    forecast = read_frozen(dataset, 'forecast-ready.jsonl')
    segmentation = read_frozen(dataset, 'segmentation-ready.jsonl')
    by_year, by_split, stations = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for row in events:
        by_year[row['day'][:4]]['events'] += 1
        by_year[row['day'][:4]]['M_plus'] += int(row.get('is_m_plus', False))
    for row in forecast:
        by_split[row['split']]['samples'] += 1
        by_split[row['split']]['positive_samples'] += row['target_m_plus']
    for row in segmentation:
        if row['category'] == 'FL':
            stations[row['split']][row['instrument']] += 1
    # This reference is a coverage counterexample, NOT an AR training label.
    day = [r for r in events if r['day'] == '2020-11-29']
    return {'created_utc': now(), 'dataset_manifest_sha256': digest(Path(dataset)/'manifest-hashes.json'),
            'events_by_year': dict(by_year), 'forecast_by_split': dict(by_split),
            'FL_stations_by_split': dict(stations),
            'counterexample': {'date': '2020-11-29', 'official_class': 'M4.4',
                'source': 'https://ntrs.nasa.gov/citations/20210017543',
                'local_events': day, 'interpretation': '不能把当前AR目录的缺项等同于全日面负例；不推断用户漏下载'},
            'gates': {
                'segmentation': {'state': 'ready', 'reason': '冻结配对及保护区已通过工程检查'},
                'AR_forecast_reference_reproduction': {'state': 'research_only',
                    'reason': '只复现已编号AR参考目录标签；不能声称独立权威完整事件监督或实时预报可用'},
                'AR_forecast_scientific_release': {'state': 'blocked',
                    'reason': '仍需逐年权威事件对照、等级/位置归属与负例覆盖核验；不能用一个反例代替全量审计'},
                'full_disk_forecast': {'state': 'blocked',
                    'reason': '完整全日面标签、观测空档和GOES跨代等级口径尚未冻结'},
                'operational_forecast': {'state': 'blocked',
                    'reason': '输入历史发布时间及未来持续监测验证未完成'}},
            'official_products': [
                'https://www.ncei.noaa.gov/products/goes-r-extreme-ultraviolet-xray-irradiance',
                'https://www.ngdc.noaa.gov/stp/space-weather/solar-data/solar-features/solar-flares/x-rays/goes/xrs/',
                'https://www.spaceweather.gov/products/solar-and-geophysical-event-reports'],
            'negative_rule': '没有完整覆盖证明的窗口不能生成新的全日面负例；不改写原始清单'}


def freeze(dataset, output):
    output = output_path(output, dataset)
    if (output/'protocol.json').exists():
        raise ValueError('协议已存在，禁止覆盖；新版本请使用新目录')
    output.mkdir(parents=True, exist_ok=True)
    configs = experiment_matrix()
    for config in configs:
        atomic_json(output/'configs'/f"{config['id']}.json", config)
    atomic_json(output/'label-audit.json', audit(dataset))
    matrix = {'schema': 1, 'created_utc': now(), 'seeds': list(SEEDS),
              'dataset': str(Path(dataset).resolve()),
              'dataset_manifest_sha256': digest(Path(dataset)/'manifest-hashes.json'),
              'recognition': configs, 'recognition_run_count': len(configs),
              'test_policy': '任何训练与开发命令不得读取test；M5 selection-lock后单独evaluate',
              'main_contrasts': ['FL-F2 vs FL-F0', 'FL-F2 vs FL-F1', 'FL-F2 vs FL-F5',
                                 'AR-A1 vs AR-A0', 'CH-C2 vs CH-C1'],
              'statistics': {'cluster_bootstrap': 2000, 'segmentation_cluster': 'UTC 27-day block',
                             'forecast_cluster': 'NOAA AR', 'multiplicity': 'Holm'},
              'resources': {'gpu_tasks': 1, 'reserved_vram_gib': 6, 'total_vram_gib': 7.5,
                            'process_tree_ram_gib': 20, 'minimum_available_ram_gib': 4},
              'forecast': {'features': ['MGB_PIL', 'R', 'MGB', 'Total_Unsigned_Flux', 'Area'],
                  'target': 'numbered AR, M1.0+, (T,T+24h]', 'mode': 'reference_catalogue_research',
                  'models': ['climate', 'logistic', 'svm', 'hist_gradient_boosting', 'lstm32', 'mlp32'],
                  'internal_folds': [[2011, 2012, 2013], [2013, 2014, 2015], [2015, 2016, 2020]],
                  'minimum_positive_per_fold': 10, 'minimum_valid_folds': 2,
                  'calibration_year': 2022, 'test_years': [2023, 2024]}}
    atomic_json(output/'protocol.json', matrix)
    atomic_json(output/'queue.json', {'state': 'not_started', 'order': [
        'FL-F2-s17', 'AR-A1-s17', 'CH-C2-s17',
        *[r['id'] for r in configs if r['id'] not in ('FL-F2-s17', 'AR-A1-s17', 'CH-C2-s17')]]})
    return matrix
