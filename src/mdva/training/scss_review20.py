"""Separate preregistered 20-epoch review; the historical8-epoch evaluator stays frozen."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import numpy as np
from PIL import Image
from .durable import atomic_json,commit
from .runtime import digest,now,exclusive,Resources,output_path
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .metrics import binary_segmentation,segmentation_summary
from .ch_display_probe import localization_record
from .localization_audit import aggregate
from .regional_decision import paired_intervals
from .checkpoint_archive import archive_checkpoint
ROOT=Path(__file__).resolve().parents[3]


def run(study,output,resume=False,checkpoint_kind="best"):
    import torch
    from .scss_main import identity
    from .scss_models import architecture
    from .reviewed_train import seed_all,load_checkpoint
    study=Path(study).resolve();protocol=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if checkpoint_kind not in ('best','last'):raise ValueError('仅允许登记的best/last检查点')
    plan_path=study/'evaluation-20-protocol-v2.json'
    plan=json.loads(plan_path.read_text(encoding='utf-8'))
    if plan['training_protocol_sha256']!=digest(study/'protocol.json') or plan['stage_sha256']!=digest(study/'active-stage.json') or plan['evaluator_sha256']!=digest(__file__):raise ValueError('20轮评价协议身份不符')
    for name,sha in plan['evaluation_code'].items():
        if digest(Path(__file__).with_name(name))!=sha:raise ValueError('20轮评价依赖代码身份改变')
    if plan['checkpoints']!=['best','last']:raise ValueError('必须保留预登记的best与last比较')
    dataset=Path(protocol['dataset']);output=output_path(output,dataset)
    status=json.loads((study/'run/status.json').read_text(encoding='utf-8'))
    if status['state'] not in ('paused','completed') or status['completed_epoch']!=20:
        raise ValueError('仅在第20轮完整保存且训练停止后评价，不抢占训练GPU')
    if protocol['code']!=identity() or protocol['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):
        raise ValueError('训练代码或数据身份改变')
    history=json.loads((study/'run/metrics.json').read_text(encoding='utf-8'))
    continuation={'gate_passed':False,'scope':'20轮结果须综合审查，不自动追加训练预算'}
    checkpoint=study/'run'/(checkpoint_kind+'.pt')
    evidence={'training_protocol_sha256':digest(study/'protocol.json'),'checkpoint_sha256':digest(checkpoint),
        'history_sha256':digest(study/'run/metrics.json'),'scope':'CH full validation only, declared best/last through20; no test/calibration','checkpoint_kind':checkpoint_kind,'evaluation_plan_sha256':digest(plan_path),
        'threshold':.5,'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_review20.py','scss_models.py','datasets.py','metrics.py','ch_display_probe.py','localization_audit.py','regional_decision.py')}}
    if (output/'identity.json').exists():
        if not resume or json.loads((output/'identity.json').read_text(encoding='utf-8'))!=evidence:raise ValueError('评价身份改变或未指定恢复')
    else:
        if output.exists() and any(output.iterdir()):raise ValueError('评价目录非空')
        output.mkdir(parents=True,exist_ok=True);atomic_json(output/'identity.json',evidence)
    data=SegmentationDataset(dataset,'CH','validation',512)
    original=ROOT/'out/recognition-improvement-20261116/display-probe-01'
    baseline_cases=json.loads((original/'cases.json').read_text(encoding='utf-8'))['original']
    if digest(original/'cases.json')!=plan['control_locations_sha256']:raise ValueError('原候选区域证据改变')
    if {r['uri'] for r in baseline_cases}!={r['uri'] for r in data.rows}:raise ValueError('比较样本集合不同')
    scores=[];baseline_scores=[];locations=[]
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        if digest(checkpoint)!=evidence['checkpoint_sha256'] or digest(study/'run/metrics.json')!=evidence['history_sha256']:
            raise ValueError('等待GPU期间检查点或训练历史改变')
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75)
        archived=archive_checkpoint(checkpoint,output/'checkpoint.pt',evidence['checkpoint_sha256'])
        saved=load_checkpoint(archived)
        expected_identity=json.loads((study/'run/identity.json').read_text(encoding='utf-8'))
        if saved['identity']!=expected_identity or saved['epoch']>20 or (checkpoint_kind=='last' and saved['epoch']!=20):raise ValueError('检查点不是登记的前20轮模型')
        if checkpoint_kind=='best' and saved['epoch']!=status['best_epoch']:raise ValueError('最佳检查点选择与训练状态不一致')
        if not all(torch.isfinite(p).all() for p in saved['model'].values()):raise ValueError('模型参数非有限')
        model,_=architecture(protocol['config']);model.load_state_dict(saved['model']);model.cuda().eval();resource=Resources()
        with torch.inference_mode():
            for i,row in enumerate(data.rows):
                if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','completed':i,'total':len(data)});return {'state':'paused'}
                sid=sample_id(row['uri']);folder=output/'cases'/sid;record_path=folder/'result.json'
                image=data.source(row['uri']);baseline=original/'cases'/sid
                control=json.loads((baseline/'result.json').read_text(encoding='utf-8'))
                if digest(baseline/'original.png')!=control['assets']['original.png']:raise ValueError('原候选缓存改变')
                if record_path.exists():
                    record=json.loads(record_path.read_text(encoding='utf-8'))
                    if record['identity_sha256']!=digest(output/'identity.json') or any(digest(folder/n)!=sha for n,sha in record['assets'].items()):raise ValueError('逐图评价缓存身份改变')
                    data.source(row['mask_uri'])
                else:
                    with Image.open(image) as im:x=np.asarray(im.convert('L'),dtype=np.float32).copy()/255.
                    if x.shape!=(512,512):raise ValueError('未登记图像网格')
                    logits=model(torch.from_numpy(x[None,None]).cuda())
                    if not torch.isfinite(logits).all():raise ValueError('输出非有限')
                    probability=logits.float().sigmoid()[0,0].cpu().numpy();prediction=probability>=.5
                    # Reference participates only after the model forward.
                    reference=np.load(data.source(row['mask_uri']),allow_pickle=False)
                    control_mask=np.asarray(Image.open(baseline/'original.png'))>0
                    control_score=binary_segmentation(control_mask,reference>0,boundary=True)
                    if any(control_score[k]!=control['scores']['original'][k] for k in ('tp','fp','fn')):raise ValueError('原候选计数回读不一致')
                    score=binary_segmentation(prediction,reference>0,boundary=True)
                    folder.mkdir(parents=True,exist_ok=True)
                    commit(folder/'prediction.png',lambda stream:Image.fromarray(prediction.astype('uint8')*255).save(stream,format='PNG'))
                    commit(folder/'probability.npz',lambda stream:np.savez_compressed(stream,probability=probability))
                    record={'uri':row['uri'],'identity_sha256':digest(output/'identity.json'),'score':score,'control_score':control_score,
                        'localization':localization_record(row,prediction,reference,score,'SCSS-GN-'+checkpoint_kind+'-through20'),
                        'image_sha256':digest(image),'assets':{n:digest(folder/n) for n in ('prediction.png','probability.npz')}}
                    atomic_json(record_path,record)
                scores.append(record['score']);baseline_scores.append(record['control_score']);locations.append(record['localization'])
                if (i+1)%50==0 or i+1==len(data):
                    atomic_json(output/'status.json',{'state':'inference_only','completed':i+1,'total':len(data),'resource':resource.sample()})
            final_resource=resource.sample()
    metrics=segmentation_summary(scores);control_metrics=segmentation_summary(baseline_scores)
    expected=(saved['validation'] if checkpoint_kind=='best' else saved['history'][-1]['validation'])['dice']
    if abs(metrics['dice']-expected)>1e-5:raise ValueError('完整验证复算与历史检查点不一致，停止结论')
    localization=aggregate(locations);gates=protocol['final_practical_gate']
    checks={'dice':metrics['dice']>=gates['dice'],'precision':metrics['precision']>=gates['precision'],
            'half_coverage':localization['reference_coverage']['half_covered_fraction']>=gates['half_coverage_fraction']}
    uncertainty=paired_intervals(baseline_cases,locations)
    uncertainty.update(difference='SCSS-GN declared-checkpoint-through20 minus CH-C2-s2026 candidate',scope='Exploratory reused validation; different configurations and training budgets, single seed.')
    result={'state':'completed','updated_utc':now(),'identity':evidence,'checkpoint_epoch':saved['epoch'],'checkpoint_policy_label':'SCSS '+checkpoint_kind+'（截至20轮）',
        'metrics':metrics,'control_metrics':control_metrics,'localization':localization,'control_localization':aggregate(baseline_cases),
        'continuation':continuation,'practical_checks':checks,'practical_gate_passed':all(checks.values()),
        'paired_uncertainty':uncertainty,'exact_replay':True,'resource':final_resource,
        'checkpoint_archive':{'file':'checkpoint.pt','sha256':digest(archived),'verified':True},
        'test_accessed':False,'calibration_accessed':False,'models_replaced':False,
        'limitations':['No independent empty-reference cohort; false-positive image rate not measured.',
            'Connected regions are not physical instances or NOAA identities.','Single-seed development results do not establish robustness or final scientific gain.']}
    result['by_year']={year:{'images':sum(row['time_utc'].startswith(year) for row in data.rows),
        'candidate':segmentation_summary([score for row,score in zip(data.rows,scores) if row['time_utc'].startswith(year)]),
        'control':segmentation_summary([score for row,score in zip(data.rows,baseline_scores) if row['time_utc'].startswith(year)])}
        for year in sorted({row['time_utc'][:4] for row in data.rows})}
    atomic_json(output/'summary.json',result);atomic_json(output/'locations.json',locations)
    atomic_json(output/'status.json',{'state':'completed','completed':len(data),'total':len(data)})
    return result


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--output',required=True);p.add_argument('--resume',action='store_true');p.add_argument('--checkpoint',choices=['best','last'],default='best');a=p.parse_args()
    r=run(a.study,a.output,a.resume,a.checkpoint);print(json.dumps({k:r[k] for k in ('state','metrics','continuation','practical_checks') if k in r}),flush=True)
