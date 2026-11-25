"""Paired AR fine-tuning with a reference-corroborated, training-only SHARP head."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import numpy as np
from scipy.ndimage import find_objects
import torch
from torch import nn
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import BUDGET, ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import append, read
from .metrics import binary_segmentation
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('control', 'sharp')
AUX_WEIGHT = .30
MIN_COVERAGE = .25
PAD = 24
BASE_IDENTITY = engine.code_identity
BASE_LOSS = engine.loss_value
LEDGER = ROOT/'out/training-20261112-v2'


def code_identity():
    directory = Path(__file__).parent
    names = ('ar_sharp_auxiliary.py','ar_sharp_assist_audit.py','ar_scale_study.py',
             'ar_scale_review.py','ar_precision.py','ar_guard_train.py','forecast_region_detector_audit.py')
    return {**BASE_IDENTITY(), **{name:digest(directory/name) for name in names}}


def guide_target(instances, sharp, missed):
    if instances.shape != (1024,1024) or sharp.shape != instances.shape or sharp.dtype != np.bool_:
        raise ValueError('辅助范围与原生参考网格不符')
    instances = instances.astype(np.int32,copy=False)
    invalid_foreground = sharp | (instances > 0)
    auxiliary = np.full(instances.shape,-1,dtype=np.int8)
    counts = Counter()
    for identity,window in enumerate(find_objects(instances),1):
        if window is None:
            continue
        region = instances[window] == identity
        overlap = region & sharp[window]
        if int(overlap.sum()) < max(4,int(np.ceil(region.sum()*MIN_COVERAGE))):
            continue
        local = tuple(slice(max(0,s.start-PAD),min(n,s.stop+PAD)) for s,n in zip(window,instances.shape,strict=True))
        auxiliary[local][~invalid_foreground[local]] = 0
        auxiliary[window][overlap] = 1
        counts['corroborated'] += 1
        counts['missed_corroborated'] += identity in missed
    cells = auxiliary.reshape(512,2,512,2)
    # Retain a positive subpixel. A negative requires a fully corroborated background cell.
    positive = (cells == 1).any(axis=(1,3))
    negative = (cells == 0).all(axis=(1,3))
    downsample = np.where(positive,1,np.where(negative,0,-1)).astype(np.int8)
    return downsample, counts


def dual_head(model):
    old = model.out
    head = nn.Conv2d(old.in_channels,2,1)
    with torch.no_grad():
        head.weight.copy_(old.weight.expand_as(head.weight))
        head.bias.copy_(old.bias.expand_as(head.bias))
    model.out = head
    base_forward = model.forward

    def forward(self,image):
        logits = base_forward(image)
        return logits if self.training else logits[:, :1]

    model.forward = MethodType(forward,model)
    return model


def training_loss(logits,target,kind):
    primary = BASE_LOSS(logits[:, :1],target[:, :1],kind)
    if target.shape[1] == 1:
        return primary
    weak = target[:, 1:2]
    aux = F.binary_cross_entropy_with_logits(logits[:,1:2].float(),weak.float().clamp_min(0),reduction='none')
    positive,negative = weak == 1,weak == 0
    return primary + .5*AUX_WEIGHT*((aux*positive).sum()/positive.sum().clamp_min(1)
                                    + (aux*negative).sum()/negative.sum().clamp_min(1))


def verified_summary(directory):
    report = read(directory/'summary.json')
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['protocol_sha256'] != digest(directory/'protocol.json')):
        raise ValueError('训练来源未完成或身份不符')
    return report,read(directory/'protocol.json')


def prepare(study, source, cohort, sharp, previous):
    origin = load_plan(source)
    study = output_path(study,origin['dataset'])
    if any(study.is_relative_to(root) for root in (source,cohort,sharp,previous)):
        raise ValueError('训练试验须使用独立目录')
    pairs,sample = verified_summary(cohort)
    receipt,selection = verified_summary(sharp)
    geometry,policy = verified_summary(previous)
    if (sample['data'] != origin['data_identity'] or selection['cohort_sha256'] != digest(cohort/'summary.json')
            or selection['manifest_sha256'] != origin['data_identity']['manifest-hashes.json']
            or selection['module_sha256'] != digest(Path(__file__).with_name('ar_sharp_assist_audit.py'))
            or selection['selection_used_reference'] or policy['cohort_sha256'] != digest(cohort/'summary.json')):
        raise ValueError('活动区队列来源不一致')
    train = SegmentationDataset(origin['dataset'],'AR','train')
    available = {r['uri']: r for r in train.rows}
    cohort_rows = [r for r in pairs['results'] if r['split']=='train']
    ids = [r['uri'] for r in cohort_rows]
    selected = {r['uri']:r for r in receipt['results']}
    if (len(ids) != 272 or len(set(ids)) != 272 or not set(ids) <= available.keys()
            or len(selected) != 96 or set(selected) != set(selection['selected']) or not selected.keys() <= set(ids)):
        raise ValueError('训练图像名单改变或跨越冻结划分')
    frozen = {
        'code': code_identity(), 'source_sha256':digest(source/'protocol.json'),
        'cohort_sha256':digest(cohort/'summary.json'),'archive_sha256':digest(sharp/'summary.json'),
        'prior_train_geometry_sha256':digest(previous/'summary.json'),
        'train_uris':ids, 'inference_input':'single HMI display magnetogram',
        'guide': {'weight':AUX_WEIGHT,'minimum_reference_fraction':MIN_COVERAGE,'padding_pixels':PAD,
                  'positive':'SHARP overlap within one corroborating human mask; unknown pixels excluded',
                  'negative':'local background outside both maps; never use missing archive as a negative',
                  'validation_guide':False},
        'config': {**configuration(origin,512),'loss':'bce_dice'},
        'gate': {'minimum_gained_zero_overlap_regions':4,'maximum_region_precision_loss':.01,
                 'maximum_region_recall_loss':.01,'maximum_dice_loss':.01},
        'test_accessed':False,'calibration_accessed':False,
    }
    identity = study/'protocol.json'
    if identity.is_file() and read(identity) != frozen:
        raise ValueError('已有试验身份发生变化')
    atomic_json(identity,frozen)
    guide_index = {}
    for i,row in enumerate(cohort_rows):
        uri = row['uri']
        item = selected.get(uri)
        if item is None or item['state']!='prepared':
            continue
        previous_case = read(previous/'train'/f'{i:04d}.json')
        if previous_case['uri'] != uri:
            raise ValueError('起点错分清单与训练图像不一致')
        fact = previous_case['geometry']
        scores = np.asarray(fact['iou']).reshape(len(fact['iou']),len(fact['reference_ids']))
        missed = {rid for rid,score in zip(fact['reference_ids'],scores.max(axis=0,initial=0),strict=True) if score == 0}
        original = sharp/'sources'/item['nominal_tai']/'mask.npy'
        if digest(original) != item['sha256'] or item['protocol_sha256'] != receipt['protocol_sha256']:
            raise ValueError('档案指导掩膜指纹改变')
        ref = np.load(train.source(available[uri]['mask_uri']),allow_pickle=False)
        weak,counts = guide_target(ref,np.load(original,allow_pickle=False),missed)
        if not counts:
            continue
        path = study/'guides'/f'{i:04d}.npy'
        existing = read(path.with_suffix('.json'))
        evidence = {'uri':uri,'source_sha256':item['sha256'],'archive_protocol_sha256':receipt['protocol_sha256'],
                    'source_reference_sha256':train.fingerprints[available[uri]['mask_uri']]['sha256'],
                    **counts,'positive_pixels':int((weak==1).sum()),'negative_pixels':int((weak==0).sum())}
        if existing is None:
            commit(path,lambda file:np.save(file,weak,allow_pickle=False))
            atomic_json(path.with_suffix('.json'),{**evidence,'sha256':digest(path)})
        elif existing != {**evidence,'sha256':digest(path)} or not np.array_equal(np.load(path,allow_pickle=False),weak):
            raise ValueError('训练辅助缓存无法复验')
        guide_index[uri] = {**evidence,'file':path.name,'sha256':digest(path)}
    if not guide_index:
        raise ValueError('没有真实参考与SHARP共同支持的训练图像')
    atomic_json(study/'guides.json',guide_index)
    summary = {'state':'prepared','images':len(guide_index),
               'corroborated_regions':sum(g['corroborated'] for g in guide_index.values()),
               'missed_corroborated_regions':sum(g['missed_corroborated'] for g in guide_index.values()),
               'selection_sha256':digest(study/'guides.json'),'archive_sha256':frozen['archive_sha256']}
    atomic_json(study/'guide-summary.json',summary)
    return origin,frozen,guide_index


def train(study, origin, plan, guides, arm):
    run = study/arm/'run'
    status = read(run/'status.json',{})
    if (study/'PAUSE').is_file() or (run/'PAUSE').is_file():
        return None
    config = {**plan['config'],'id':f'AR-SHARP-{arm}-s2026',
              'protocol_sha256':digest(study/'protocol.json'),'guides_sha256':digest(study/'guides.json')}
    if status.get('state') == 'completed' and status.get('completed_epoch') == BUDGET:
        return config
    model,base = warm_model(origin,config)
    model = dual_head(model)

    class Guided(SegmentationDataset):
        def __init__(self,directory,category,split,size):
            super().__init__(directory,category,split,size)
            self.training_split = split == 'train'
            self.checked = set()
            if self.training_split:
                index = {r['uri']:r for r in self.rows}
                self.rows = [index[uri] for uri in plan['train_uris']]

        def __getitem__(self,index):
            item = super().__getitem__(index)
            if not self.training_split:
                return item
            target = torch.full_like(item['semantic_mask'],-1)
            record = guides.get(item['uri']) if arm == 'sharp' else None
            if record is not None:
                file = study/'guides'/record['file']
                if file not in self.checked:
                    if digest(file) != record['sha256']:
                        raise ValueError('训练辅助范围已改变')
                    self.checked.add(file)
                target = torch.from_numpy(np.load(file,allow_pickle=False).astype(np.float32))[None]
            item['semantic_mask'] = torch.cat((item['semantic_mask'],target))
            return item

    class TorchOptions:
        optim = SimpleNamespace(Adam=lambda p,**kw:torch.optim.Adam(p,**{**kw,'lr':config['learning_rate']}),
                                lr_scheduler=torch.optim.lr_scheduler)

        def __getattr__(self,name):
            return getattr(torch,name)

    previous = (engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loss_value,engine.torch)
    append(LEDGER,'development','活动区 SHARP 辅助学习',
           f'{arm} 组开始。两组使用相同单图输入、人工标注、训练图与固定三轮预算。',run_id=config['id'])
    try:
        engine.architecture = lambda _: (model,{**base,'train_only_guide': arm=='sharp'})
        engine.code_identity = code_identity
        engine.SegmentationDataset = Guided
        engine.loss_value = training_loss
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        state = engine.train(config,origin['dataset'],run,resume=(run/'last.pt').exists())
        append(LEDGER,'development','活动区配对训练保存',
               f'{arm} 已保存 {state["completed_epoch"]}/{BUDGET} 轮。',run_id=config['id'],epoch=state['completed_epoch'])
        return config if state['state']=='completed' else None
    finally:
        (engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loss_value,engine.torch) = previous


def seen_instances(prediction, labels):
    assigned = set(np.unique(labels[prediction]).tolist())-{0}
    return sorted(set(np.unique(labels).tolist())-{0}-assigned)


def validate(study, origin, frozen, configs):
    data = SegmentationDataset(origin['dataset'],'AR','validation',512)
    if len(data) != 276:
        raise ValueError('开发图清单不对应冻结版本')
    models = {'starting': warm_model(origin,frozen['config'])[0]}
    history,checkpoints = {},{}
    for arm,config in configs.items():
        path = study/arm/'run/last.pt'
        state = engine.load_checkpoint(path)
        identity = read(path.parent/'identity.json')
        if (state['epoch']!=BUDGET or state['identity']!=identity or identity['config']!=config
                or identity['code']!=frozen['code']):
            raise ValueError('最后检查点与配对对照身份不符')
        model = dual_head(warm_model(origin,config)[0])
        model.load_state_dict(state['model'])
        models[arm] = model
        history[arm] = state['history'][-1]['validation']['dice']
        checkpoints[arm] = digest(path)
    records = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        engine.seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resources = Resources()
        for model in models.values():
            model.cuda().eval()
        with torch.inference_mode():
            for i,row in enumerate(data.rows):
                x = data[i]['image'][None].cuda()
                raw = np.load(data.source(row['mask_uri']),allow_pickle=False)
                arms = {}
                for name,model in models.items():
                    with torch.autocast('cuda',dtype=torch.float16):
                        logits = model(x)
                    if logits.shape[1]!=1 or not torch.isfinite(logits).all():
                        raise ValueError('单图开发推理身份或数值异常')
                    y = F.interpolate(logits.float().sigmoid(),size=raw.shape,mode='bilinear',
                                      align_corners=False)[0,0].cpu().numpy()
                    mask = filter_by_area(y >= .4,64)
                    arms[name] = {'objects':native_objects(mask,raw),'pixels':binary_segmentation(y>=.5,raw>0),
                                  'entirely_missed':seen_instances(mask,raw)}
                records.append({'uri':row['uri'],'time_utc':row['time_utc'],'arms':arms})
                if i % 24 == 0:
                    resources.sample()
        usage = resources.sample()
    scores = {name: estimate(records,name) for name in models}
    for name,metric in scores.items():
        metric['completely_missed_regions'] = sum(len(r['arms'][name]['entirely_missed']) for r in records)
    replay = {arm:scores[arm]['pixels']['dice']-history[arm] for arm in ARMS}
    if any(abs(delta)>1e-5 for delta in replay.values()):
        raise ValueError(f'检查点重放不一致 {replay}')
    control,sharp = (scores[name] for name in ARMS)
    gained = control['completely_missed_regions']-sharp['completely_missed_regions']
    gate = frozen['gate']
    passed = (gained >= gate['minimum_gained_zero_overlap_regions']
              and sharp['region_precision'] >= control['region_precision']-gate['maximum_region_precision_loss']
              and sharp['region_recall'] >= control['region_recall']-gate['maximum_region_recall_loss']
              and sharp['pixels']['dice'] >= control['pixels']['dice']-gate['maximum_dice_loss'])
    atomic_json(study/'images.json',records)
    result = {'state':'completed','created_utc':now(),'images':len(records),'results':scores,
              'net_reduction_entirely_missed':gained,'pilot_gate_passed':passed,'resource':usage,
              'checkpoint_sha256':checkpoints,'validation_replay_delta':replay,
              'images_sha256':digest(study/'images.json'),'protocol_sha256':digest(study/'protocol.json'),
              'guide_summary_sha256':digest(study/'guide-summary.json'),
              'test_accessed':False,'calibration_accessed':False,'deployed':False}
    atomic_json(study/'summary.json',result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study','source','cohort','sharp','previous'):
        parser.add_argument('--'+key,required=True,type=Path)
    parser.add_argument('--prepare-only',action='store_true')
    arguments = parser.parse_args()
    configure_environment()
    study,source,cohort,sharp,previous = (getattr(arguments,key).resolve() for key in ('study','source','cohort','sharp','previous'))
    origin,protocol,guides = prepare(study,source,cohort,sharp,previous)
    if arguments.prepare_only:
        print(json.dumps(read(study/'guide-summary.json')))
        return
    configs = {}
    for arm in ARMS:
        configs[arm] = train(study,origin,protocol,guides,arm)
        if configs[arm] is None:
            return
    result = validate(study,origin,protocol,configs)
    print(json.dumps({key:result[key] for key in ('state','net_reduction_entirely_missed','pilot_gate_passed')},ensure_ascii=False))


if __name__ == '__main__':
    main()
