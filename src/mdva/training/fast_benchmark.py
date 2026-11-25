"""Serial real-model throughput and exactness checks, never modifies production runs."""
from pathlib import Path
import argparse
import copy
import gc
import json
import statistics
import time

from .durable import atomic_json
from .runtime import exclusive,Resources,now


def equal(a,b):
    import torch
    import numpy as np
    if torch.is_tensor(a):return torch.equal(a,b)
    if isinstance(a,np.ndarray):return np.array_equal(a,b)
    if isinstance(a,dict):return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,(list,tuple)):return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    return a==b


def segment(data,loader,config,workers):
    import torch
    from . import reviewed_train as engine
    config={**config,'data_workers':workers}
    engine.seed_all(config['seed']);torch.cuda.reset_peak_memory_stats()
    model,_=engine.architecture(config);model.cuda();model.train()
    optimizer=torch.optim.Adam(model.parameters(),lr=1e-3)
    scaler=torch.amp.GradScaler('cuda')
    rng=engine.rng_state();initial=copy.deepcopy(model.state_dict())
    x=torch.zeros(2,1,512,512,device='cuda')
    with torch.autocast('cuda',dtype=torch.float16):loss=engine.loss_value(model(x),x,config['loss'])
    scaler.scale(loss).backward();optimizer.zero_grad(set_to_none=True)
    model.load_state_dict(initial);del initial,x,loss;engine.restore_rng(rng)
    resource=Resources();losses=[];uris=[]
    torch.cuda.synchronize();started=time.perf_counter()
    batches=loader(data,config,0,2)
    for step,batch in enumerate(batches):
        x,y=batch['image'].cuda(),batch['semantic_mask'].cuda()
        if config.get('augment'):
            gain=.9+.2*torch.rand((len(x),1,1,1),device='cuda');gamma=.9+.2*torch.rand((len(x),1,1,1),device='cuda')
            x=(x*gain).clamp(0,1).pow(gamma)
        with torch.autocast('cuda',dtype=torch.float16):loss=engine.loss_value(model(x),y,config['loss'])
        if not torch.isfinite(loss):raise ValueError('速度对照出现非有限损失')
        window=min(8,len(data)-(step//4)*8)
        scaler.scale(loss*len(x)/window).backward()
        if (step+1)%4==0 or step+1==len(batches):
            scaler.step(optimizer);scaler.update();optimizer.zero_grad(set_to_none=True)
        losses.append(float(loss.detach()));uris.extend(batch['uri'])
        if step%32==0:resource.sample()
    torch.cuda.synchronize();seconds=time.perf_counter()-started
    saved={'model':{k:v.cpu().clone() for k,v in model.state_dict().items()},'optimizer':copy.deepcopy(optimizer.state_dict()),
           'rng':engine.rng_state(),'scaler':scaler.state_dict(),'losses':losses,'uris':uris}
    # Move optimizer state to CPU before releasing the GPU for the next variant.
    for state in saved['optimizer']['state'].values():
        for key,value in state.items():
            if torch.is_tensor(value):state[key]=value.cpu()
    perf={'seconds':seconds,'images':len(data),'images_per_second':len(data)/seconds,'workers':workers,'resource':resource.sample()}
    del model,optimizer,scaler,batches,x,y,loss
    gc.collect();torch.cuda.empty_cache()
    return perf,saved


def run(experiment,output,category):
    import torch
    from . import reviewed_train as engine
    from .datasets import SegmentationDataset
    from .fast_data import CachedDataset,loader_for,DEFAULT_CACHE
    from .run_locations import run_folder
    from .continuation import lock_alive
    experiment=Path(experiment);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if not (experiment/'PAUSE').exists() or lock_alive(experiment/'queue.lock') is not False:
        raise ValueError('速度测试要求正式队列已暂停并退出')
    cfg_id='AR-A1-s2026' if category=='AR' else 'CH-C2-s42'
    config=json.loads((experiment/'revisions/io-reviewed-v1/configs'/(cfg_id+'.json')).read_text(encoding='utf-8'))
    dataset=Path(json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))['dataset'])
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        original=SegmentationDataset(dataset,category,'train',512)
        started=time.perf_counter();cached=CachedDataset(dataset,category,'train',512,cache_root=DEFAULT_CACHE)
        initialization=time.perf_counter()-started
        original.rows=original.rows[:257];cached.rows=cached.rows[:257]
        for row in original.rows:original.source(row['uri']);original.source(row['mask_uri'])
        baseline=None;timings=[]
        for mode,workers in [('original',0),('cached',0),('cached',2),('cached',4)]:
            for trial in range(2):
                perf,state=segment(original if mode=='original' else cached,
                    engine.loader_for if mode=='original' else loader_for,config,workers)
                if baseline is None:baseline=state
                same=equal(baseline,state)
                if not same:raise ValueError(f'{category}/{mode}/{workers}训练数值、顺序或随机状态不一致')
                row={**perf,'mode':mode,'trial':trial,'exact_payload_equal':same};timings.append(row)
                atomic_json(output/f'{category}-progress.json',{'state':'benchmarking','last':row,'updated_utc':now()})
                print(json.dumps({'category':category,**row}),flush=True)
                del state
        reference=statistics.median(r['seconds'] for r in timings if r['mode']=='original')
        choices=[{'workers':w,'seconds':statistics.median(r['seconds'] for r in timings if r['mode']=='cached' and r['workers']==w)} for w in (0,2,4)]
        chosen=min(choices,key=lambda r:r['seconds'])
        # Full validation, same best checkpoint and per-image evaluation order.
        identifier='AR-A1-s17' if category=='AR' else 'CH-C2-s17'
        folder=run_folder(experiment,identifier)
        best=engine.load_checkpoint(folder/'best.pt');validation_config=best['identity']['config']
        engine.seed_all(validation_config['seed']);model,_=engine.architecture(validation_config)
        model.load_state_dict(best['model']);model.cuda()
        val=SegmentationDataset(dataset,category,'validation',512)
        fast_val=CachedDataset(dataset,category,'validation',512,cache_root=DEFAULT_CACHE)
        started=time.perf_counter();a,arows=engine.evaluate(model,val,validation_config);old_val_seconds=time.perf_counter()-started
        started=time.perf_counter();b,brows=engine.evaluate(model,fast_val,validation_config);fast_val_seconds=time.perf_counter()-started
        if not equal(a,b) or not equal(arows,brows):raise ValueError('完整验证结果不一致')
        if abs(a['dice']-best['validation']['dice'])>1e-5:raise ValueError('历史验证指标复算不一致')
        result={'category':category,'finished_utc':now(),'mode':'same_microbatch_2_effective_batch_8',
            'training_images':257,'repeats_per_variant':2,'trials':timings,'baseline_seconds':reference,'chosen':chosen,
            'training_speedup_factor':reference/chosen['seconds'],'source_and_cache_init_seconds':initialization,
            'validation_images':len(val),'validation_equal':True,'validation_dice':a['dice'],
            'validation_seconds_original':old_val_seconds,'validation_seconds_cached':fast_val_seconds,
            'adopt':chosen['seconds']<=reference*.9,'test_accessed':False,
            'limitation':'257图短时吞吐对照，含worker启动；正式整轮速度与缓存首次开销另计'}
        atomic_json(output/f'{category}-benchmark.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='trials'}),flush=True)


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser()
    p.add_argument('--experiment',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--category',choices=['AR','CH'],required=True)
    args=p.parse_args();run(args.experiment,args.output,args.category)
