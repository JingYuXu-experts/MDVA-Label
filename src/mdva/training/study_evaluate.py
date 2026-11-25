"""Complete validation-only localization check for the isolated CH-H0 experiment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .durable import atomic_json
from .runtime import digest,exclusive,now,output_path,Resources
from .localization_audit import objects,coverage,match_boxes,aggregate


def evaluate(study,dataset,output,checkpoint='best'):
    import torch
    from .datasets import SegmentationDataset
    from .metrics import binary_segmentation,segmentation_summary
    study,dataset=Path(study).resolve(),Path(dataset).resolve()
    output=output_path(output,dataset)
    if output.is_relative_to(study):raise ValueError('评价输出不能写入训练运行')
    if output.exists() and any(output.iterdir()):raise ValueError('评价版本已存在')
    config=json.loads((study/'identity.json').read_text(encoding='utf-8'))['config']
    if checkpoint not in ('best','last'):raise ValueError('检查点只允许best/last')
    if config['id']=='CH-H0-s2026':
        from .author_train import code_identity,architecture
    elif config['id'] in ('CH-R0-s2026','CH-R1-s2026'):
        from .regional_train import code_identity,architecture
        if checkpoint!='last':raise ValueError('区域损失对照固定评价预算末轮last')
    else:raise ValueError('未登记的对照模型')
    if config['category']!='CH':raise ValueError('对照任务不符')
    lock=Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'
    with exclusive(lock):
        saved=torch.load(study/(checkpoint+'.pt'),map_location='cpu',weights_only=False)
        if saved['identity']['code']!=code_identity():raise ValueError('模型来源代码已变化')
        from .reviewed_train import data_identity
        if saved['identity']['data']!=data_identity(dataset):raise ValueError('数据身份不符')
        model,_=architecture(config);model.load_state_dict(saved['model'])
        if any(not torch.isfinite(p).all() for p in model.parameters()):raise ValueError('权重非有限')
        torch.cuda.set_per_process_memory_fraction(.75)
        model.cuda().eval();resource=Resources()
        data=SegmentationDataset(dataset,'CH','validation',config['size'])
        output.mkdir(parents=True)
        (output/'masks').mkdir()
        fixed_uris={data.rows[i]['uri'] for i in np.linspace(0,len(data)-1,8,dtype=int)}
        figures=[]
        results=[];metrics=[]
        with torch.inference_mode():
            for index,row in enumerate(data.rows):
                item=data[index]
                logits=model(item['image'][None].cuda())  # Reference never enters model.
                if not torch.isfinite(logits).all():raise ValueError('推理非有限')
                mask=np.load(data.source(row['mask_uri']),allow_pickle=False)
                p=torch.nn.functional.interpolate(logits.float().sigmoid(),mask.shape,mode='bilinear',align_corners=False)[0,0].cpu().numpy()
                pred=p>=.5
                from .diagnostic_support import sample_id
                from PIL import Image
                sid=sample_id(row['uri'])
                Image.fromarray(pred.astype('uint8')*255).save(output/'masks'/(sid+'.png'))
                if row['uri'] in fixed_uris:
                    import matplotlib
                    matplotlib.use('Agg')
                    from matplotlib import pyplot as plt
                    with Image.open(data.source(row['uri'])) as im:original=np.asarray(im.convert('L'))
                    fig,axes=plt.subplots(1,4,figsize=(13,3.4))
                    error=np.zeros((*pred.shape,3),dtype=np.uint8)
                    error[pred & (mask>0)]=[90,205,125];error[pred & (mask==0)]=[245,80,95];error[~pred & (mask>0)]=[255,190,35]
                    for ax,image,title in zip(axes,[original,mask>0,pred,error],['Input image','Reference','Automatic prediction','Green TP / Red FP / Yellow FN']):
                        ax.imshow(image,cmap='gray');ax.set_title(title,fontsize=9);ax.axis('off')
                    fig.suptitle(f'{config["id"]} development validation | {row["time_utc"]}',fontsize=10)
                    fig.tight_layout();fig.savefig(output/(sid+'.png'),dpi=130);plt.close(fig)
                    figures.append({'uri':row['uri'],'figure':sid+'.png'})
                score=binary_segmentation(pred,mask>0,boundary=True);metrics.append(score)
                predicted=objects(pred);reference=objects(mask)
                covered,supported=coverage(pred,mask)
                for region,fraction in zip(reference,covered):
                    scaled=region['pixels']*512**2/mask.size
                    region.update(covered_fraction=fraction,size_bin='small_lt64' if scaled<64 else 'medium_64_511' if scaled<512 else 'large_ge512')
                for region,fraction in zip(predicted,supported):region['reference_overlap_fraction']=fraction
                results.append({'run_id':config['id'],'uri':row['uri'],'mask_uri':row['mask_uri'],'split':'validation',
                    'instrument':row['instrument'],'time_utc':row['time_utc'],'dice':score['dice'],
                    'reference':reference,'predicted':predicted,
                    'matches':{str(t):match_boxes(predicted,reference,t) for t in (.3,.5)}})
                if index%50==0:
                    resource.sample();atomic_json(output/'status.json',{'state':'running','completed':index+1,'total':len(data),'updated_utc':now()})
        result={'state':'completed','split':'validation','threshold':.5,'model':config['id'],
            'epoch':saved['epoch'],'checkpoint':checkpoint,'checkpoint_sha256':digest(study/(checkpoint+'.pt')),
            'identity_sha256':digest(study/'identity.json'),'evaluator_sha256':digest(__file__),
            'metrics':segmentation_summary(metrics),'localization':aggregate(results),'resources':resource.sample(),
            'fixed_chronological_figures':figures,
            'test_accessed':False,'limitations':['single seed development comparison, no independent superiority claim',
                'compare equal budgets separately from historical best checkpoints','no independent negative images']}
        atomic_json(output/'summary.json',result);atomic_json(output/'cases.json',results)
        atomic_json(output/'status.json',{'state':'completed','completed':len(data),'total':len(data),'updated_utc':now()})
        return result


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--dataset',required=True);p.add_argument('--output',required=True)
    p.add_argument('--checkpoint',choices=['best','last'],default='best')
    a=p.parse_args();print(json.dumps(evaluate(a.study,a.dataset,a.output,a.checkpoint),ensure_ascii=True))
