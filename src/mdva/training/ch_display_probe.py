"""One fixed-weight, validation-only display correction experiment; no optimizer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .ch_display_audit import normalize
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .inference import load_model
from .localization_audit import objects,coverage,match_boxes,aggregate
from .metrics import binary_segmentation,segmentation_summary
from .regional_decision import paired_intervals
from .runtime import digest,exclusive,now,output_path,Resources

ROOT=Path(__file__).resolve().parents[3]


def validate_preparation(preparation,dataset):
    protocol=json.loads((preparation/'protocol.json').read_text(encoding='utf-8'))
    if protocol['anchor_fit_split']!='train' or protocol['category']!='CH' or protocol['threshold']!=.5:
        raise ValueError('仅允许训练锚点、CH、固定阈值0.5的预登记方案')
    if digest(dataset/'manifest-hashes.json')!=protocol['frozen_manifest_sha256']:
        raise ValueError('冻结数据清单改变')
    if digest(preparation/'images.jsonl')!=protocol['input_rows_sha256']:
        raise ValueError('输入审计结果改变')
    if set(protocol['code'])!={'ch_display_audit.py','spatial_audit.py'}:
        raise ValueError('预处理代码登记不完整')
    for name,sha in protocol['code'].items():
        if name not in ('ch_display_audit.py','spatial_audit.py') or digest(Path(__file__).with_name(name))!=sha:
            raise ValueError('预处理代码身份改变，不能继续原方案')
    return protocol


def localization_record(row,pred,mask,score,arm):
    predicted=objects(pred);reference=objects(mask)
    covered,supported=coverage(pred,mask)
    for region,fraction in zip(reference,covered):
        n=region['pixels']*512**2/mask.size
        region.update(covered_fraction=fraction,size_bin='small_lt64' if n<64 else 'medium_64_511' if n<512 else 'large_ge512')
    for region,fraction in zip(predicted,supported):region['reference_overlap_fraction']=fraction
    return {'uri':row['uri'],'time_utc':row['time_utc'],'year':row['time_utc'][:4],
        'run_id':arm,'dice':score['dice'],'reference':reference,'predicted':predicted,
        'matches':{str(t):match_boxes(predicted,reference,t) for t in (.3,.5)}}


def run(preparation,dataset,output,resume=False):
    import torch
    from .train import seed_all
    preparation,dataset=Path(preparation).resolve(),Path(dataset).resolve()
    output=output_path(output,dataset)
    if output.is_relative_to(preparation):raise ValueError('评价输出不能写入输入审计目录')
    protocol=validate_preparation(preparation,dataset)
    package=ROOT/'out/training-20261112-v2/packages/CH-C2-s2026'
    card=json.loads((package/'model.json').read_text(encoding='utf-8'))
    historical=ROOT/'out/model-diagnostics/20261116-final/case-index.json'
    index=json.loads(historical.read_text(encoding='utf-8'))
    expected={r['uri']:r for r in index if r['run_id']=='CH-C2-s2026' and r['diagnostic_subset']=='validation'}
    if len(expected)!=526:raise ValueError('原始完整验证结果数量不符')
    identity={'scope':'CH full validation only, no training/no calibration/no test',
        'protocol_sha256':digest(preparation/'protocol.json'),'source_model':'CH-C2-s2026',
        'weights_sha256':card['weights_sha256'],'model_card_sha256':digest(package/'model.json'),
        'historical_index_sha256':digest(historical),'evaluator_sha256':digest(__file__),
        'threshold':.5,'arms':['original','normalized'],'anchor':protocol['anchor'],
        'gates':{'half_coverage_gain':.03,'relative_zero_overlap_reduction':.05,
                 'max_dice_drop':.01,'max_precision_drop':.03}}
    if output.exists() and any(output.iterdir()):
        if not resume or json.loads((output/'identity.json').read_text(encoding='utf-8'))!=identity:
            raise ValueError('评价输出已存在或身份不同，拒绝覆盖')
    else:
        output.mkdir(parents=True);atomic_json(output/'identity.json',identity)
        (output/'source').mkdir()
        for name in ('ch_display_probe.py','ch_display_audit.py','spatial_audit.py'):
            (output/'source'/name).write_bytes(Path(__file__).with_name(name).read_bytes())
    data=SegmentationDataset(dataset,'CH','validation',512)
    if {r['uri'] for r in data.rows}!=set(expected):raise ValueError('比较图像集合不同')
    arms={a:[] for a in identity['arms']};scores={a:[] for a in arms};adjustments=[]
    from .journal import append
    experiment=ROOT/'out/training-20261112-v2'
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);model,card=load_model(package);resource=Resources()
        with torch.inference_mode():
            for i,row in enumerate(data.rows):
                if (output/'PAUSE').exists():
                    atomic_json(output/'status.json',{'state':'paused','completed':i,'total':len(data),'updated_utc':now()})
                    return {'state':'paused'}
                sid=sample_id(row['uri']);folder=output/'cases'/sid
                saved=folder/'result.json'
                image_path=data.source(row['uri'])
                if saved.exists():
                    record=json.loads(saved.read_text(encoding='utf-8'))
                    if record['identity_sha256']!=digest(output/'identity.json'):
                        raise ValueError('逐图缓存来源不符')
                    if any(digest(folder/name)!=sha for name,sha in record['assets'].items()):
                        raise ValueError('逐图缓存损坏，不能跳过')
                    data.source(row['mask_uri'])
                else:
                    with Image.open(image_path) as image:gray=np.asarray(image.convert('L'))
                    if gray.shape!=(512,512):raise ValueError('本方案仅支持已核验512原生显示图')
                    adjusted,adjustment=normalize(gray,protocol['anchor'])
                    predictions={}
                    for arm,x in (('original',gray.astype(np.float32)/255.),('normalized',adjusted)):
                        logits=model(torch.from_numpy(x.copy()[None,None]).cuda())
                        if not torch.isfinite(logits).all():raise ValueError('非有限推理，不当作背景')
                        probability=logits.float().sigmoid()[0,0].cpu().numpy()
                        predictions[arm]=probability>=.5
                    # Reference is read only AFTER both model forwards.
                    mask=np.load(data.source(row['mask_uri']),allow_pickle=False)
                    record={'uri':row['uri'],'identity_sha256':digest(output/'identity.json'),
                            'adjustment':adjustment,'scores':{},'localization':{},'assets':{}}
                    folder.mkdir(parents=True,exist_ok=True)
                    for arm,prediction in predictions.items():
                        score=binary_segmentation(prediction,mask>0,boundary=True)
                        if arm=='original' and any(score[k]!=expected[row['uri']][k] for k in ('tp','fp','fn')):
                            raise ValueError('原输入重放不匹配历史像素计数，先排查口径，停止比较')
                        record['scores'][arm]=score
                        record['localization'][arm]=localization_record(row,prediction,mask,score,arm)
                        name=arm+'.png';Image.fromarray(prediction.astype('uint8')*255).save(folder/name)
                        record['assets'][name]=digest(folder/name)
                    atomic_json(saved,record)
                for arm in arms:
                    arms[arm].append(record['localization'][arm]);scores[arm].append(record['scores'][arm])
                adjustments.append(record['adjustment'])
                if i%100==0 or i+1==len(data):
                    progress={'state':'inference_only','completed':i+1,'total':len(data),'updated_utc':now(),
                              'training':False,'resource':resource.sample()}
                    atomic_json(output/'status.json',progress)
                    print(json.dumps(progress,ensure_ascii=False),flush=True)
                    append(experiment,'development','CH输入亮度对照推理',f'{i+1}/{len(data)}图，原输入与标准化双对照；没有模型训练。',
                           details={'status':str(output/'status.json')})
    metrics={a:segmentation_summary(scores[a]) for a in arms}
    loc={a:aggregate(arms[a]) for a in arms}
    original,corrected=loc['original']['reference_coverage'],loc['normalized']['reference_coverage']
    limits=identity['gates']
    checks={'half_coverage_gain':corrected['half_covered_fraction']>=original['half_covered_fraction']+limits['half_coverage_gain'],
        'zero_overlap_reduction':corrected['zero_overlap']<=original['zero_overlap']*(1-limits['relative_zero_overlap_reduction']),
        'dice_preserved':metrics['normalized']['dice']>=metrics['original']['dice']-limits['max_dice_drop'],
        'precision_preserved':metrics['normalized']['precision']>=metrics['original']['precision']-limits['max_precision_drop']}
    uncertainty=paired_intervals(arms['original'],arms['normalized'])
    uncertainty.update(difference='normalized minus original',scope='paired development inference comparison, one frozen model')
    report={'state':'completed','updated_utc':now(),'identity':identity,'metrics':metrics,'localization':loc,
        'normalization_applied':sum(a['applied'] for a in adjustments),'normalization_fallback':sum(not a['applied'] for a in adjustments),
        'control_pixel_counts_exact':True,'checks':checks,'gate_passed':all(checks.values()),
        'decision':'targeted_validation_required' if all(checks.values()) else 'do_not_deploy_or_expand_this_transform',
        'paired_uncertainty':uncertainty,'resource':resource.sample(),'weights_changed':False,
        'test_opened':False,'calibration_opened':False,'model_deployed':False,
        'by_year':{year:{a:aggregate([r for r in arms[a] if r['year']==year]) for a in arms}
                   for year in sorted({r['year'] for r in arms['original']})}}
    atomic_json(output/'summary.json',report)
    atomic_json(output/'cases.json',arms)
    atomic_json(output/'status.json',{'state':'completed','completed':len(data),'total':len(data),'updated_utc':now(),'training':False})
    append(experiment,'handoff','CH亮度标准化对照已结束',report['decision'],details={'report':str(output/'summary.json')})
    return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    parser=argparse.ArgumentParser();parser.add_argument('--preparation',required=True);parser.add_argument('--dataset',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--resume',action='store_true')
    a=parser.parse_args();print(json.dumps(run(a.preparation,a.dataset,a.output,a.resume),ensure_ascii=False))
