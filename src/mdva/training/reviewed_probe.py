"""Bounded FL train-only learning probe; never a formal validation result."""
from pathlib import Path
import json

from .durable import atomic_json, atomic_checkpoint
from .diagnostic_support import read
from .runtime import exclusive, Resources, now


def prepare_review(experiment):
    import numpy as np
    experiment=Path(experiment)
    previous=experiment/'fl-review-samples.json'
    if previous.exists():
        return read(previous)
    source=experiment.parent/'model-diagnostics/20261115-03'
    rows=read(source/'samples.json')
    rng=np.random.default_rng(2026);selected=[];probe=[]
    for station in ('BBSO_Halpha','HSOS_Halpha','KSO_Halpha'):
        for split in ('train','validation'):
            group=[r for r in rows if r['category']=='FL' and r['instrument']==station and r['split']==split
                   and r['diagnostic_subset']!='train_empty_reference']
            group.sort(key=lambda r:(sum(int(n) for v,n in r['mask_statistics']['pixels'].items() if int(v)>0),r['uri']))
            indices=np.array_split(np.arange(len(group)),3)
            chosen=[group[int(i)] for ids,n in zip(indices,(4,3,3)) for i in rng.choice(ids,n,replace=False)]
            selected.extend(chosen)
            if split=='train':
                probe.extend(chosen[:2]+chosen[4:6]+chosen[7:9 if station=='BBSO_Halpha' else 8])
    value={'seed':2026,'review_images':selected,'probe_images':probe,'professional_review_completed':False,
           'review_status':'自动分层与错误证据已准备；标注范围和方向仍待专业复核',
           'test_accessed':False,'probe_uses_training_pixels_only':True}
    assert len(selected)==60 and len(probe)==16
    assert all(r['split']=='train' for r in probe)
    atomic_json(previous,value)
    return value


def run(experiment):
    import torch
    from . import reviewed_train as trainer
    from .datasets import SegmentationDataset
    experiment=Path(experiment);folder=experiment/'revisions/io-reviewed-v1/FL-learning-probe'
    if read(folder/'status.json',{}).get('state')=='completed':
        return
    selection=prepare_review(experiment)
    config={**read(experiment/'configs/FL-F0-s17.json'),'augment':False,'station_balance':False,
            'id':'FL-learning-probe','execution_revision':'io-reviewed-v1'}
    dataset=Path(read(experiment/'protocol.json')['dataset'])
    folder.mkdir(parents=True,exist_ok=True)
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        trainer.seed_all(17);torch.cuda.set_per_process_memory_fraction(.75)
        data=SegmentationDataset(dataset,'FL','train',512)
        by_uri={r['uri']:r for r in data.rows}
        assert all(r['uri'] in by_uri for r in selection['probe_images'])
        data.rows=[by_uri[r['uri']] for r in selection['probe_images']]
        model,_=trainer.architecture(config);model.cuda()
        optimizer=torch.optim.Adam(model.parameters(),lr=1e-3)
        scaler=torch.amp.GradScaler('cuda')
        resource=Resources();history=[];step=0
        identity={'config':config,'code':trainer.code_identity(),'data':trainer.data_identity(dataset),
                  'uris':[r['uri'] for r in data.rows]}
        if (folder/'last.pt').exists():
            saved=trainer.load_checkpoint(folder/'last.pt')
            if saved['identity']!=identity:raise ValueError('FL诊断身份变化，禁止混用恢复')
            model.load_state_dict(saved['model']);optimizer.load_state_dict(saved['optimizer']);scaler.load_state_dict(saved['scaler'])
            for state in optimizer.state.values():
                for k,v in state.items():
                    if torch.is_tensor(v) and k!='step':state[k]=v.cuda()
            trainer.restore_rng(saved['rng']);step=saved['step'];history=saved['history']
        def save():
            atomic_checkpoint(folder/'last.pt',{'step':step,'model':model.state_dict(),'optimizer':optimizer.state_dict(),
                'scaler':scaler.state_dict(),'rng':trainer.rng_state(),'history':history,'identity':identity})
        while step<300:
            if (experiment/'PAUSE').exists():
                save();atomic_json(folder/'status.json',{'state':'paused','step':step,'updated_utc':now()});return
            model.train();optimizer.zero_grad(set_to_none=True)
            indices=torch.randperm(16)[:8].tolist()
            for offset in range(0,8,2):
                items=[data[i] for i in indices[offset:offset+2]]
                x=torch.stack([r['image'] for r in items]).cuda();y=torch.stack([r['semantic_mask'] for r in items]).cuda()
                with torch.autocast('cuda',dtype=torch.float16):loss=trainer.loss_value(model(x),y,config['loss'])
                if not torch.isfinite(loss):raise ValueError('FL学习检查出现非有限损失')
                scaler.scale(loss/4).backward()
            scaler.step(optimizer);scaler.update();step+=1
            if step%25==0:
                summary,_=trainer.evaluate(model,data,config,resource=resource)
                history.append({'optimizer_step':step,'training_sample_dice':summary['dice'],'loss':summary['loss']})
                save()
                atomic_json(folder/'status.json',{'state':'running','step':step,'max_steps':300,'updated_utc':now(),
                    'formal':False,'training_sample_dice':summary['dice'],'resource':resource.sample()})
                print(json.dumps(history[-1]),flush=True)
                if summary['dice']>=.9:break
        best=max(r['training_sample_dice'] for r in history)
        result={'state':'completed','formal':False,'step':step,'best_training_sample_dice':best,'learning_reference_met':best>=.9,
            'finding':'16图可学习性参考线通过；不代表验证泛化' if best>=.9 else
            '300步内未达到0.90参考线；FL新增实验暂缓，继续核查学习设置、细结构与标签语义，不能据此认定代码错误',
            'test_accessed':False,'validation_used_for_fitting':False,'updated_utc':now(),'resource':resource.sample()}
        atomic_json(folder/'status.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    import argparse
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True)
    run(p.parse_args().experiment)
