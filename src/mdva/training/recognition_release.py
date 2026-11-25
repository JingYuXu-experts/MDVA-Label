"""Explicit, reviewable AR/CH evaluation scope; no implicit test authorization."""
from __future__ import annotations

from pathlib import Path
import json

from .diagnostic_support import read
from .durable import atomic_json
from .runtime import digest, now

FAMILIES = ('AR-A0', 'AR-A1', 'CH-C1', 'CH-C2')
SEEDS = (17, 42, 2026)
EVALUATION_IDS = tuple(f'{family}-s{seed}' for family in FAMILIES for seed in SEEDS)
CANDIDATES = {'AR': 'AR-A0-s42', 'CH': 'CH-C2-s2026'}
PROTOCOL = 'recognition-evaluation-protocol.json'
CONFIRMATION = 'recognition-test-authorization.json'


def scope_counts(protocol, runs, approved):
    """Preserve original counts while displaying the completed approved scope."""
    ids = {c['id'] for c in protocol['recognition']}
    deferred = sorted(i for i in ids if i.startswith('CH-C0-')) if approved else []
    included = ids-set(deferred)
    completed = {r['id'] for r in runs if r['state'] == 'completed'}
    return {'approved_completed': len(completed & included), 'approved_total': len(included),
            'deferred_total': len(deferred), 'deferred_ids': deferred,
            'approved_scope_complete': bool(included) and included <= completed}


def checkpoint_evidence(experiment, identifier):
    from .run_locations import run_folder, model_and_identity
    from .reviewed_train import data_identity
    experiment = Path(experiment)
    folder = run_folder(experiment, identifier)
    state, identity = read(folder/'status.json'), read(folder/'identity.json')
    if not state or state.get('state') != 'completed' or not state.get('formal'):
        raise ValueError('只有完整正式运行可以进入冻结清单：'+identifier)
    config = read(folder.parent.parent/'configs'/(identifier+'.json'))
    if identity['config'] != config:
        raise ValueError('检查点配置与登记不符：'+identifier)
    _, expected = model_and_identity(config)
    dataset = Path(read(experiment/'protocol.json')['dataset'])
    if identity['code'] != expected() or identity['data'] != data_identity(dataset):
        raise ValueError('代码或数据身份已改变：'+identifier)
    if config != next(c for c in read(folder.parent.parent/'protocol.json')['recognition'] if c['id'] == identifier):
        raise ValueError('修订协议与配置不符')
    return {'checkpoint_sha256':digest(folder/'best.pt'), 'identity_sha256':digest(folder/'identity.json'),
            'metrics_sha256':digest(folder/'metrics.json'), 'run_directory':folder.relative_to(experiment).as_posix(),
            'configuration': config, 'data':identity['data'], 'training_code':identity['code']}


def prepare_protocol(experiment):
    experiment = Path(experiment)
    protocol = read(experiment/'protocol.json')
    if not set(EVALUATION_IDS) <= {c['id'] for c in protocol['recognition']}:
        raise ValueError('缺少完整AR/CH四配置三种子，不能缩减评价范围')
    evidence = {i:checkpoint_evidence(experiment,i) for i in EVALUATION_IDS}
    dataset = Path(protocol['dataset'])
    rows = [json.loads(line) for line in (dataset/'segmentation-ready.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    counts = {category:sum(r['eligible'] and r['category']==category and r['split']=='test' for r in rows)
              for category in ('AR','CH')}
    evaluator = Path(__file__).parent
    value = {'schema':1, 'state':'awaiting_user_confirmation', 'created_utc':now(),
             'original_protocol_sha256':digest(experiment/'protocol.json'),
             'data_manifest_sha256':digest(dataset/'manifest-hashes.json'),
             'source_fingerprints_sha256':digest(dataset/'source-files.jsonl'),
             'recognition':evidence, 'candidates':CANDIDATES, 'threshold':.5,
             'test_images_by_task':counts, 'model_image_evaluations':sum(counts.values())*6,
             'primary_comparisons':[['AR-A0','AR-A1'],['CH-C1','CH-C2']],
             'bootstrap':{'unit':'UTC 27-day block','repeats':2000,'seed':2026,
                          'seed_aggregation':'per-image three-seed mean','multiple_comparisons':'Holm over two primary contrasts'},
             'evaluation_code':{n:digest(evaluator/n) for n in ('delivery.py','recognition_release.py','metrics.py')},
             'excluded':{'FL':'test remains sealed','CH-C0':'deferred original configurations'},
             'test_selection_forbidden':True,'test_pixels_opened':False,
             'candidate_rule':'AR-A0 and CH-C2; median validation seed in each family; never change based on test'}
    target = experiment/PROTOCOL
    if target.exists():
        old = read(target)
        if {k:v for k,v in old.items() if k!='created_utc'} != {k:v for k,v in value.items() if k!='created_utc'}:
            raise ValueError('待确认冻结清单已存在且内容变化，须建立新评价协议版本')
        return old
    atomic_json(target,value)
    return value


def authorize(experiment, protocol_hash):
    """Called only after the user separately confirms the displayed protocol."""
    experiment = Path(experiment)
    if digest(experiment/PROTOCOL) != protocol_hash:
        raise ValueError('确认的协议SHA不匹配')
    if (experiment/CONFIRMATION).exists():
        raise ValueError('确认记录已存在，禁止覆盖')
    atomic_json(experiment/CONFIRMATION, {'confirmed_utc':now(),'protocol_sha256':protocol_hash,
                'scope':'12 frozen AR/CH runs only','source':'explicit_user_confirmation'})


def validate_authorized_scope(experiment):
    experiment = Path(experiment)
    value, confirmation = read(experiment/PROTOCOL), read(experiment/CONFIRMATION)
    if not value or not confirmation or confirmation.get('protocol_sha256') != digest(experiment/PROTOCOL):
        raise ValueError('识别最终测试仍封存：需先验收冻结清单并单独确认')
    if set(value['recognition']) != set(EVALUATION_IDS) or value['candidates'] != CANDIDATES or value['threshold'] != .5:
        raise ValueError('评价范围、候选或阈值不符')
    if value['original_protocol_sha256'] != digest(experiment/'protocol.json'):
        raise ValueError('原协议已变化')
    dataset = Path(read(experiment/'protocol.json')['dataset'])
    if (value['data_manifest_sha256'] != digest(dataset/'manifest-hashes.json') or
        value['source_fingerprints_sha256'] != digest(dataset/'source-files.jsonl')):
        raise ValueError('评价数据身份已变化')
    for name,h in value['evaluation_code'].items():
        if digest(Path(__file__).parent/name) != h:
            raise ValueError('评价代码变化，禁止沿用确认')
    for identifier, expected in value['recognition'].items():
        if checkpoint_evidence(experiment,identifier) != expected:
            raise ValueError('冻结模型身份变化：'+identifier)
    return value
