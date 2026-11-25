"""Separate development evaluation, irreversible test unlock and model packaging."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .runtime import atomic_json, code_identity, digest, exclusive, now, output_path
from .run_locations import run_folder, model_and_identity


def selection_lock(experiment):
    experiment = Path(experiment)
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    if (experiment/'selection-lock.json').exists():
        raise ValueError('正式测试锁已存在，禁止覆盖')
    if (experiment/'recognition-evaluation-protocol.json').exists():
        from .recognition_release import validate_authorized_scope, PROTOCOL
        scope = validate_authorized_scope(experiment)
        value = {'locked_utc':now(), 'protocol_sha256':digest(experiment/'protocol.json'),
                 'evaluation_protocol_sha256':digest(experiment/PROTOCOL),
                 'recognition':scope['recognition'], 'candidates':scope['candidates'],
                 'test_selection_forbidden':True,'rule':'已单独确认的12项AR/CH固定评价；FL保持封存'}
        from .durable import atomic_json as commit
        commit(experiment/'selection-lock.json',value)
        return value
    evidence = {}
    for config in protocol['recognition']:
        run = run_folder(experiment, config['id'])
        status = json.loads((run/'status.json').read_text(encoding='utf-8'))
        if status['state'] != 'completed':
            raise ValueError(f"{config['id']}尚未完成；不能提前打开正式测试")
        identity = json.loads((run/'identity.json').read_text(encoding='utf-8'))
        _, expected_identity = model_and_identity(identity['config'])
        if identity['code'] != expected_identity():
            raise ValueError('运行代码与当前评价代码不同，需审计后建立新协议')
        evidence[config['id']] = {'checkpoint_sha256': digest(run/'best.pt'),
                                 'identity_sha256': digest(run/'identity.json'),
                                 'metrics_sha256': digest(run/'metrics.json'),
                                 'run_directory': run.relative_to(experiment).as_posix()}
    value = {'locked_utc': now(), 'protocol_sha256': digest(experiment/'protocol.json'),
             'recognition': evidence, 'test_selection_forbidden': True,
             'rule': '所有预注册识别配置完成后统一评价；不得据测试分数回头选配置'}
    atomic_json(experiment/'selection-lock.json', value)
    return value


def evaluate_run(experiment, identifier, split):
    experiment = Path(experiment)
    if (experiment/'recognition-evaluation-protocol.json').exists() and split in ('test','calibration'):
        from .recognition_release import validate_authorized_scope, PROTOCOL
        scope = validate_authorized_scope(experiment)
        if split != 'test' or identifier not in scope['recognition']:
            raise ValueError('此评价协议仅放行12项AR/CH测试；FL与校准集保持封存')
        lock = json.loads((experiment/'selection-lock.json').read_text(encoding='utf-8'))
        if lock.get('evaluation_protocol_sha256') != digest(experiment/PROTOCOL):
            raise ValueError('测试锁与已确认评价协议不符')
    import torch
    from .datasets import SegmentationDataset
    from .train import data_identity, evaluate, load_checkpoint, seed_all
    from .runtime import Resources
    run = run_folder(experiment, identifier)
    identity = json.loads((run/'identity.json').read_text(encoding='utf-8'))
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    dataset = Path(protocol['dataset'])
    architecture, expected_identity = model_and_identity(identity['config'])
    if identity['code'] != expected_identity() or identity['data'] != data_identity(dataset):
        raise ValueError('代码或数据身份不符，不能将新版本结果混入原实验')
    if split == 'test':
        lock = json.loads((experiment/'selection-lock.json').read_text(encoding='utf-8'))
        if lock['protocol_sha256'] != digest(experiment/'protocol.json'):
            raise ValueError('测试协议已经改变')
        if lock['recognition'][identifier]['checkpoint_sha256'] != digest(run/'best.pt'):
            raise ValueError('正式测试后模型发生变化，拒绝覆写结果')
    destination = run/'evaluation'/split
    if (destination/'summary.json').exists():
        raise ValueError('评价已存在，禁止覆写；保留首次结果')
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        seed_all(identity['config']['seed'])
        torch.cuda.set_per_process_memory_fraction(.75)
        model, _ = architecture(identity['config'])
        checkpoint = load_checkpoint(run/'best.pt')
        model.load_state_dict(checkpoint['model'])
        model.cuda()
        samples = SegmentationDataset(dataset, identity['config']['category'], split, identity['config']['size'])
        resource = Resources()
        summary, rows = evaluate(model, samples, identity['config'], boundary=True, resource=resource)
        summary.update(split=split, run_id=identifier, epoch=checkpoint['epoch'],
                       checkpoint_sha256=digest(run/'best.pt'), resource=resource.sample())
        atomic_json(destination/'images.json', rows)
        atomic_json(destination/'summary.json', summary)
        return summary


def package(experiment, identifier):
    import os
    import uuid
    import torch
    from safetensors.torch import save_file
    from .train import load_checkpoint
    run = run_folder(experiment, identifier)
    identity = json.loads((run/'identity.json').read_text(encoding='utf-8'))
    architecture, expected_identity = model_and_identity(identity['config'])
    if identity['code'] != expected_identity():
        raise ValueError('模型训练代码身份变化，不能打包')
    from .train import data_identity
    if identity['data'] != data_identity(Path(json.loads((Path(experiment)/'protocol.json').read_text(encoding='utf-8'))['dataset'])):
        raise ValueError('模型数据身份变化，不能打包')
    state = json.loads((run/'status.json').read_text(encoding='utf-8'))
    if state['state'] != 'completed' or not state['formal']:
        raise ValueError('仅可打包完整正式运行；诊断权重不能作为产品模型')
    destination = Path(experiment)/'packages'/identifier
    if destination.exists():
        raise ValueError('模型包已存在，禁止覆盖')
    final_destination = destination
    destination = destination.with_name('.building-'+identifier+'-'+uuid.uuid4().hex)
    destination.mkdir(parents=True)
    checkpoint = load_checkpoint(run/'best.pt')
    if checkpoint['identity'] != identity or not all(torch.isfinite(t).all() for t in checkpoint['model'].values()):
        raise ValueError('检查点身份或参数异常，不能打包')
    weights = {k: t.cpu().contiguous() for k, t in checkpoint['model'].items()}
    save_file(weights, str(destination/'weights.safetensors'))
    category = identity['config']['category']
    card = {'id': identifier, 'task': 'solar_semantic_segmentation', 'category': category,
            'release_state': 'research_candidate', 'config': identity['config'],
            'weights_sha256': digest(destination/'weights.safetensors'), 'data': identity['data'],
            'training_code': identity['code'], 'epoch': checkpoint['epoch'],
            'validation': checkpoint['validation'], 'resource': state['resource'],
            'input': {'AR': 'HMI显示域磁图，非物理场强FITS；MDI需单独验证',
                      'CH': 'AIA193显示域图像', 'FL': 'Hα全日面图像'}[category],
            'preprocessing': 'Pillow L; /255; bilinear resize; native-grid probability restore',
            'threshold': .5, 'original_mask_is_immutable': True,
            'input_condition': {'AR':'magnetogram','CH':'AIA193','FL':'Halpha'}[category],
            'output': '模型语义候选；连通分量不等同NOAA对象身份；必须人工复核',
            'scientific_test_complete': (run/'evaluation/test/summary.json').exists(),
            'test_state':'completed' if (run/'evaluation/test/summary.json').exists() else 'awaiting_confirmation',
            'confidence_scope':'区域平均模型分数，未作概率校准',
            'license_note':'遵循项目实现与训练数据原有条款；本地研究候选包不包含原始数据，不作额外授权',
            'architecture_provenance': json.loads((run/'architecture.json').read_text(encoding='utf-8')),
            'created_utc': now()}
    if category == 'FL':
        import sys
        licensefile = Path(sys.prefix)/'model_sources/flat_unet/LICENSE'
        if identity['config']['model'] == 'flat_unet':
            (destination/'AUTHOR-LICENSE').write_bytes(licensefile.read_bytes())
    atomic_json(destination/'model.json', card)
    (destination/'MODEL_CARD.md').write_text(
        f"# {identifier}\n\n状态：研究候选，人工辅助使用。\n\n"
        f"任务：{category} 语义分割；原始真值保持不变。\n\n"
        "模型输出不能代替太阳活动物理分类或NOAA身份。模型元数据、原生网格验证指标、"
        "数据和权重哈希见 model.json。正式测试状态以该文件及评价目录为准。\n",
        encoding='utf-8')
    os.replace(destination,final_destination)
    return card


def report(experiment):
    """Generate tables only from existing results; leave unrun cells blank."""
    import csv
    from .__main__ import status
    progress = status(experiment)
    atomic_json(experiment/'progress.json', progress)
    output = experiment/'reports'
    output.mkdir(exist_ok=True)
    with (output/'recognition.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['id', 'state', 'completed_epoch', 'best_epoch', 'validation_dice', 'test_dice'])
        writer.writeheader()
        for row in progress['runs']:
            testfile = run_folder(experiment, row['id'])/'evaluation/test/summary.json'
            test = json.loads(testfile.read_text(encoding='utf-8')) if testfile.exists() else {}
            writer.writerow({'id': row['id'], 'state': row['state'], 'completed_epoch': row.get('completed_epoch'),
                             'best_epoch': row.get('best_epoch'), 'validation_dice': row.get('best_validation_dice'),
                             'test_dice': test.get('dice')})
    from .paper import generate
    from .model_cards import forecast_cards
    from .context import active_forecast
    result = generate(experiment)
    return {'report': str(output), 'completed_runs': progress['complete'],
            'forecast_model_cards': forecast_cards(active_forecast(experiment)), **result}


def dispatch(command, experiment, identifier=None, split='validation'):
    experiment = Path(experiment).resolve()
    if identifier is not None and not re.fullmatch(r'(AR-A[01]|CH-C[012]|FL-F[0-8])-s(17|42|2026)', identifier):
        raise ValueError('实验编号不合法')
    if command == 'lock-test':
        result = selection_lock(experiment)
    elif command == 'report':
        result = report(experiment)
    elif command == 'evaluate' and identifier:
        result = evaluate_run(experiment, identifier, split)
    elif command == 'package' and identifier:
        result = package(experiment, identifier)
    else:
        raise ValueError('需要--run-id')
    print(json.dumps(result, ensure_ascii=False, indent=2))
