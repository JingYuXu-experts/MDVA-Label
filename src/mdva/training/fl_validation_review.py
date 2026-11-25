"""Fixed five-checkpoint development validation, never final-test evaluation."""
import argparse,gc,json
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.ndimage import label
from .datasets import SegmentationDataset
from .fl_author_model import AuthorGraph,SOURCE
from .metrics import binary_segmentation,segmentation_summary
from .diagnostic_support import connected_scores,sample_id
from .runtime import digest,now,output_path,exclusive,Resources
from .durable import atomic_json,commit
ROOT=Path(__file__).resolve().parents[3]
BASE=ROOT/'out/recognition-improvement-20261118'
MODELS=('none-w1','none-w10','batch-w1','batch-w10','batch-w10-lr0.0001')

def validation_data(dataset,split='validation'):
    if split!='validation':raise ValueError('本入口只允许开发验证集，拒绝训练、校准和最终测试')
    return SegmentationDataset(dataset,'FL','validation',512)

def sources():
    return {m:BASE/'fl-learning-review-01'/(m+'.safetensors') for m in MODELS[:-1]}|{MODELS[-1]:BASE/'fl-rate-review-01/model.safetensors'}

def code():
    return {n:digest(Path(__file__).with_name(n)) for n in ('fl_validation_review.py','fl_author_model.py','datasets.py','metrics.py','diagnostic_support.py','runtime.py','durable.py')}

def prepare(output):
    p=json.loads((BASE/'fl-learning-01/protocol.json').read_text(encoding='utf-8'))
    data=validation_data(p['dataset']);output=output_path(output,p['dataset'])
    if output.exists():raise ValueError('验证报告必须新建，不覆盖')
    if len(data)!=182:raise ValueError('完整验证清单数量不符')
    source=sources()
    for model,path in source.items():
        directory=path.parent;report=json.loads((directory/'summary.json').read_text(encoding='utf-8'))
        expected=report['checkpoints'][model]['safe_weight_sha256'] if model in MODELS[:-1] else report['safe_weight_sha256']
        if digest(path)!=expected:raise ValueError('安全权重身份不符')
    plan={'created_utc':now(),'scope':'All182 FL development-validation images, five fixed step300 models; no optimization, threshold search, calibration or final test',
        'dataset':p['dataset'],'manifest_sha256':digest(Path(p['dataset'])/'manifest-hashes.json'),'models':{m:digest(path) for m,path in source.items()},'code':code(),
        'source_provenance_sha256':digest(SOURCE/'provenance.json'),'threshold':.5,'split':'validation','region_match':'8-connected semantic regions, maskIoU>=0.5; not independently identified physical objects',
        'purpose':'Measure unseen-to-these-checkpoints performance before proposing further training. Reused development validation is not an independent publication test.',
        'baseline_index_sha256':digest(ROOT/'out/model-diagnostics/20261116-final/case-index.json')}
    output.mkdir(parents=True);atomic_json(output/'protocol.json',plan);return plan

def run(output):
    import torch
    from safetensors.torch import load_file
    from .reviewed_train import seed_all
    output=Path(output).resolve();p=json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    if p['split']!='validation' or p['threshold']!=.5 or tuple(p['models'])!=MODELS or p['code']!=code():raise ValueError('验证范围、模型或代码改变')
    if p['manifest_sha256']!=digest(Path(p['dataset'])/'manifest-hashes.json') or p['source_provenance_sha256']!=digest(SOURCE/'provenance.json'):raise ValueError('数据或作者来源改变')
    output_path(output,p['dataset']);data=validation_data(p['dataset']);results={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False;torch.cuda.set_per_process_memory_fraction(.75)
        for name,path in sources().items():
            if digest(path)!=p['models'][name]:raise ValueError('模型身份改变')
            folder=output/name;folder.mkdir(exist_ok=True);model=AuthorGraph('none' if name.startswith('none') else 'batch')
            model.load_state_dict(load_file(path));model.cuda().eval();resource=Resources();scores=[]
            with torch.inference_mode():
                for i,row in enumerate(data.rows):
                    if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','model':name,'completed':i,'total':len(data)});return {'state':'paused'}
                    item=data[i];source=data.source(row['uri']);mask_source=data.source(row['mask_uri']);sid=sample_id(row['uri']);record_path=folder/(sid+'.json')
                    if record_path.exists():
                        record=json.loads(record_path.read_text(encoding='utf-8'))
                        if record['protocol_sha256']!=digest(output/'protocol.json') or record['image_sha256']!=digest(source) or record['mask_sha256']!=digest(mask_source):raise ValueError('缓存来源改变')
                        for asset,sha in record['assets'].items():
                            if digest(folder/asset)!=sha:raise ValueError('缓存资源改变')
                    else:
                        logits=model(item['image'][None].cuda())
                        if not torch.isfinite(logits).all():raise ValueError('输出非有限')
                        prob=logits.sigmoid()[0,0].cpu().numpy();mask=np.load(mask_source,allow_pickle=False)>0
                        if mask.shape!=prob.shape:raise ValueError('原生网格不同')
                        prediction=prob>=.5;score=binary_segmentation(prediction,mask,boundary=True)
                        regions=connected_scores(prediction,label(mask,structure=np.ones((3,3)))[0])
                        commit(folder/(sid+'.npz'),lambda stream:np.savez_compressed(stream,probability=prob))
                        commit(folder/(sid+'.png'),lambda stream:Image.fromarray(prediction.astype('uint8')*255).save(stream,format='PNG'))
                        record={'uri':row['uri'],'instrument':row['instrument'],'score':score,'regions':regions,'protocol_sha256':digest(output/'protocol.json'),
                            'image_sha256':digest(source),'mask_sha256':digest(mask_source),'assets':{sid+suffix:digest(folder/(sid+suffix)) for suffix in ('.npz','.png')}}
                        atomic_json(record_path,record)
                    scores.append(record)
                    if (i+1)%25==0 or i+1==len(data):atomic_json(output/'status.json',{'state':'validation_inference','model':name,'completed':i+1,'total':len(data),'resource':resource.sample()})
            totals={k:sum(r['regions'][k] for r in scores) for k in ('reference_regions','predicted_components','matched','missed_regions','false_positive_regions')}
            totals.update(precision=totals['matched']/totals['predicted_components'] if totals['predicted_components'] else 0.,recall=totals['matched']/totals['reference_regions'] if totals['reference_regions'] else None)
            result={'metrics':segmentation_summary([r['score'] for r in scores]),'regions':totals,
                'stations':{s:segmentation_summary([r['score'] for r in scores if r['instrument']==s]) for s in sorted({r['instrument'] for r in scores})},'resource':resource.sample()}
            results[name]=result;atomic_json(folder/'summary.json',result);print(json.dumps({'model':name,**result['metrics'],'regions':totals}),flush=True)
            model.cpu();del model;gc.collect();torch.cuda.empty_cache()
    baseline_path=ROOT/'out/model-diagnostics/20261116-final/case-index.json'
    if digest(baseline_path)!=p['baseline_index_sha256']:raise ValueError('原基线记录改变')
    baseline=[r for r in json.loads(baseline_path.read_text(encoding='utf-8')) if r['run_id']=='FL-F0-s2026' and r['diagnostic_subset']=='validation']
    if {r['uri'] for r in baseline}!={r['uri'] for r in data.rows}:raise ValueError('原基线比较集合不同')
    report={'created_utc':now(),'state':'completed','protocol_sha256':digest(output/'protocol.json'),'results':results,'original_full_trained_baseline':segmentation_summary(baseline),
        'scope':p['scope'],'limitations':['Small16-image training and original full-trained model have different training budgets. This is diagnostic, not a fair final architecture ranking.',
            'Connected semantic regions are not independently verified physical instances. No independent target-free cohort FPR.'],
        'test_accessed':False,'calibration_accessed':False,'models_replaced':False}
    atomic_json(output/'summary.json',report);atomic_json(output/'status.json',{'state':'completed','models':len(results),'images_per_model':182});return report

if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));p.add_argument('--output',required=True);a=p.parse_args()
    r=prepare(a.output) if a.action=='prepare' else run(a.output);print(json.dumps({'state':r.get('state')}))
