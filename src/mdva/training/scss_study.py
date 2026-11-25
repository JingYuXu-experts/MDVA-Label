"""Bounded SCSS BN/GN study, explicit gates and durable progress on the training page."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import time

import numpy as np
from PIL import Image

from .durable import atomic_json,atomic_checkpoint
from .runtime import digest,now,exclusive,Resources,output_path

ROOT=Path(__file__).resolve().parents[3]
EXPERIMENT=ROOT/'out/training-20261112-v2'
STUDY='scss-normalization-20261116'


def emit(root,arm,phase,state,step,total,**details):
    from .journal import append
    path=Path(root)/'page-progress.json'
    progress=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'id':STUDY,'arms':{}}
    progress.update(updated_utc=now(),active_arm=arm,phase=phase,state=state)
    progress['arms'][arm]={'phase':phase,'state':state,'step':step,'total':total,'updated_utc':now(),**details}
    atomic_json(path,progress)
    append(EXPERIMENT,'development','独立架构实验：SCSS '+arm,
           f'{phase} · {state} · {step}/{total}；原39项和原模型包保持原样。',details={'architecture_study':progress})


def prepare(dataset,root):
    from .datasets import SegmentationDataset
    dataset=Path(dataset).resolve();root=output_path(root,dataset)
    if root.exists():raise ValueError('独立方案需要新目录，不覆盖历史实验')
    data=SegmentationDataset(dataset,'CH','train')
    area=lambda r:sum(int(v) for k,v in r['mask_statistics']['pixels'].items() if int(k)>0)
    ordered=sorted(data.rows,key=lambda r:(area(r),r['uri']));rng=np.random.default_rng(2026)
    selected=[ordered[int(i)]['uri'] for b in np.array_split(np.arange(len(ordered)),4) for i in sorted(rng.choice(b,4,replace=False))]
    protocol={'id':STUDY,'created_utc':now(),'target':'CH input-distribution and small-batch stability; published SCSS baseline versus explicit GroupNorm adaptation',
        'author_commit':'d70e09c0187376ab1ff983a6ab864e7d089635f5','author_source_git_blob':'d5b118305264c10778f6038a787cca5d195d955f',
        'paper':'https://doi.org/10.1093/mnras/stab2536','normalization_paper':'https://arxiv.org/abs/1803.08494',
        'arms':['batch','group'],'shared':{'width':32,'depth':4,'dropout':.5,'size':512,'seed':2026,
            'loss':'bce_dice','learning_rate':.001,'micro_batch':2,'effective_batch':8,'precision':'FP32 forward; TF32 disabled',
            'preprocessing':'unchanged frozen Pillow L /255,512 bilinear; native labels nearest',
            'augmentation':'same local bounded gain/gamma recipe in main experiments; no rotation/flip'},
        'probe':{'uris':selected,'optimizer_steps':200,'save_interval':25,'scope':'train only, no generalization claim',
                 'gate':{'train_dice':.85,'all_gradients_finite':True,'max_reserved_gib':6}},
        'main_pilot':{'per_arm_epochs':8,'max_epochs_if_later_justified':40,
            'continue_gate':'After8: Dice>=.75 and epoch5..8 mean exceeds epoch1..4 mean by .02, OR Dice>=.79. Resource and finite gates also required.',
            'norm_selection':'Paired full-validation recall/precision/coverage review; no architecture claim from training sample fit alone.'},
        'comparison_warning':'Local adaptation, not paper reproduction. Validation is reused exploratory development; no original test/calibration access. No automatic model replacement.',
        'dataset':str(dataset),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_models.py','scss_study.py','fast_data.py','datasets.py','metrics.py','reviewed_train.py','durable.py')},
        'original_model_weights':{n:json.loads((EXPERIMENT/'packages'/n/'model.json').read_text(encoding='utf-8'))['weights_sha256'] for n in ('AR-A0-s42','CH-C2-s2026')}}
    root.mkdir(parents=True);atomic_json(root/'protocol.json',protocol)
    for n in protocol['code']:
        file=root/'source'/n;file.parent.mkdir(exist_ok=True);file.write_bytes(Path(__file__).with_name(n).read_bytes())
    emit(root,'batch','准备','ready',0,200,message='两组各16训练样本检查，达到学习门槛后才开始完整训练。')
    return protocol


def read_protocol(root,dataset):
    root=Path(root);p=json.loads((root/'protocol.json').read_text(encoding='utf-8'))
    if p['id']!=STUDY or p['manifest_sha256']!=digest(Path(dataset)/'manifest-hashes.json'):raise ValueError('方案或数据身份不符')
    for n,sha in p['code'].items():
        if digest(Path(__file__).with_name(n))!=sha:raise ValueError('源代码改变，新建方案版本')
    return p


def probe(dataset,root,arm,resume=False):
    import torch
    from .scss_models import SCSSNet
    from .fast_data import CachedDataset
    from .reviewed_train import seed_all,rng_state,restore_rng,load_checkpoint,loss_value,evaluate
    dataset=Path(dataset).resolve();root=output_path(root,dataset);p=read_protocol(root,dataset)
    if arm not in p['arms']:raise ValueError('未登记归一化组')
    folder=root/('probe-'+arm);folder.mkdir(exist_ok=True)
    identity={'protocol_sha256':digest(root/'protocol.json'),'arm':arm,'task':'train-only learning check'}
    status_path=folder/'status.json'
    if (folder/'last.pt').exists() and not resume:raise ValueError('已有检查点，仅允许核验身份后恢复')
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats()
        data=CachedDataset(dataset,'CH','train',512);by_uri={r['uri']:r for r in data.rows};data.rows=[by_uri[u] for u in p['probe']['uris']]
        model=SCSSNet(arm).cuda();optimizer=torch.optim.Adam(model.parameters(),lr=.001)
        step=0;history=[];resource=Resources();stopping={'value':False};handlers={}
        for sig in (signal.SIGINT,signal.SIGTERM):
            handlers[sig]=signal.signal(sig,lambda *_:stopping.update(value=True))
        if resume:
            saved=load_checkpoint(folder/'last.pt')
            if saved['identity']!=identity:raise ValueError('检查点身份不符')
            model.load_state_dict(saved['model']);optimizer.load_state_dict(saved['optimizer']);restore_rng(saved['rng'])
            for state in optimizer.state.values():
                for k,v in state.items():
                    if torch.is_tensor(v) and k!='step':state[k]=v.cuda()
            step=saved['step'];history=saved['history']
        def save():
            atomic_checkpoint(folder/'last.pt',{'identity':identity,'step':step,'history':history,'model':model.state_dict(),
                'optimizer':optimizer.state_dict(),'rng':rng_state()})
        total=p['probe']['optimizer_steps'];last_heartbeat=0
        if not resume:save()
        emit(root,arm,'16图学习检查','running',step,total,pid=os.getpid(),resource=resource.sample())
        atomic_json(folder/'identity.json',identity)
        config={'loss':'bce_dice','category':'CH'}
        try:
            while step<total:
                if stopping['value'] or (root/'PAUSE').exists() or (folder/'PAUSE').exists():
                    save();emit(root,arm,'16图学习检查','paused',step,total);atomic_json(status_path,{'state':'paused','step':step});return {'state':'paused'}
                model.train();optimizer.zero_grad(set_to_none=True)
                # Independent order RNG: same selected images and batches for both architectures.
                indices=np.random.default_rng(2026+step).choice(16,8,replace=False).tolist()
                for start in range(0,8,2):
                    items=[data[i] for i in indices[start:start+2]]
                    x=torch.stack([i['image'] for i in items]).cuda();y=torch.stack([i['semantic_mask'] for i in items]).cuda()
                    loss=loss_value(model(x),y,'bce_dice')
                    if not torch.isfinite(loss):raise ValueError('学习检查出现非有限损失')
                    (loss/4).backward()
                if not all(t.grad is not None and torch.isfinite(t.grad).all() for t in model.parameters()):raise ValueError('学习检查梯度无效')
                optimizer.step();step+=1
                if step%25==0:
                    result,_=evaluate(model,data,config,resource=resource)
                    history.append({'step':step,'train_dice':result['dice'],'loss':result['loss']});save()
                    atomic_json(folder/'history.json',history)
                    print(json.dumps({'arm':arm,**history[-1]},ensure_ascii=False),flush=True)
                    emit(root,arm,'16图学习检查','running',step,total,train_dice=result['dice'],loss=result['loss'],resource=resource.sample())
                    last_heartbeat=time.monotonic()
                elif time.monotonic()-last_heartbeat>10:
                    emit(root,arm,'16图学习检查','running',step,total,train_dice=history[-1]['train_dice'] if history else None,resource=resource.sample())
                    last_heartbeat=time.monotonic()
            best=max(r['train_dice'] for r in history)
            result={'state':'completed','step':step,'best_train_dice':best,'last_train_dice':history[-1]['train_dice'],
                'gate_passed':history[-1]['train_dice']>=p['probe']['gate']['train_dice'],'resource':resource.sample(),
                'validation_accessed':False,'test_accessed':False,'identity':identity}
            atomic_json(status_path,result);emit(root,arm,'16图学习检查','completed',step,total,**{k:result[k] for k in ('gate_passed','best_train_dice','last_train_dice','resource')})
            return result
        except Exception as exc:
            atomic_json(status_path,{'state':'failed','step':step,'error':str(exc),'last_saved_step':history[-1]['step'] if history else 0})
            emit(root,arm,'16图学习检查','failed',step,total,error=str(exc));raise
        finally:
            for sig,h in handlers.items():signal.signal(sig,h)


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    a=argparse.ArgumentParser();a.add_argument('action',choices=['prepare','probe']);a.add_argument('--dataset',required=True);a.add_argument('--root',required=True)
    a.add_argument('--arm',choices=['batch','group']);a.add_argument('--resume',action='store_true');v=a.parse_args()
    result=prepare(v.dataset,v.root) if v.action=='prepare' else probe(v.dataset,v.root,v.arm,v.resume)
    print(json.dumps({k:result[k] for k in ('state','gate_passed','last_train_dice') if k in result},ensure_ascii=False),flush=True)
