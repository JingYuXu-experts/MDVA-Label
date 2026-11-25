"""Durable inference queues with one running shard per processing device."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import argparse
import gc
import json
import os
import sqlite3
import time
import uuid

from .runtime import configure_environment, exclusive, digest, now
from .durable import atomic_json

ROOT=Path(__file__).resolve().parents[3]


def read(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default


def persist(folder,job):
    job['updated_utc']=now();job['pid']=os.getpid()
    atomic_json(folder/'job.json',job)
    # Short transaction; no annotation writes and no network/GPU work inside it.
    connection=sqlite3.connect(job['workspace_db'],timeout=30)
    try:
        report={'job_id':job['id'],'completed':job['completed'],'total':job['total'],
                'model_id':job['model_id'],'message':job.get('message'),'results':job['results']}
        state='done' if job['state'] in ('completed','completed_with_errors') else 'failed' if job['state']=='failed' else 'running'
        with connection:connection.execute('UPDATE task SET state=?,report=?,updated=? WHERE id=?',
            (state,json.dumps(report,ensure_ascii=False),time.time(),job['task_id']))
    finally:connection.close()


def cached_result(folder,row,item):
    if row.get('state')!='completed':return False
    target=folder/f"image-{row['index']:06d}"
    result=target/'candidates.json'
    return (result.is_file() and digest(result)==row['result_sha256']
            and read(result)['image_sha256']==item['sha256']
            and all((target/name).is_file() and digest(target/name)==h for name,h in row['asset_hashes'].items()))


def execute(folder,job,device='cuda'):
    import torch
    from .inference import load_model,predict_loaded
    from .runtime import Resources
    package=Path(job['package']).resolve()
    if digest(package/'weights.safetensors')!=job['weights_sha256']:
        raise ValueError('排队期间模型身份变化，请创建新任务')
    job.update(state='running', device=device, load_started_at=time.time(), active_index=None,
               message='正在加载识别模型')
    persist(folder,job)
    started=time.monotonic()
    model,card=load_model(package,device=device)
    job.pop('load_started_at',None)
    job['load_seconds']=time.monotonic()-started
    resource=Resources(device);resource.sample()
    try:
        previous={r['index']:r for r in job['results']}
        job['state']='running';job['message']='模型推理中；不读取参考掩膜、不训练、不自动保存人工标注'
        persist(folder,job)
        for index,item in enumerate(job['inputs']):
            if (index in previous and Path(item['path']).is_file() and digest(item['path'])==item['sha256']
                    and cached_result(folder,previous[index],item)):continue
            target=(folder/f'image-{index:06d}').resolve()
            if not target.is_relative_to(folder.resolve()):raise ValueError('候选输出越界')
            if target.exists():
                archive=folder/(target.name+'.interrupted-'+uuid.uuid4().hex)
                target.rename(archive)
            job.update(active_index=index,active_started_at=time.time())
            persist(folder,job)
            started=time.monotonic()
            try:
                if digest(item['path'])!=item['sha256']:raise ValueError('排队期间原图变化，请创建新任务')
                result=predict_loaded(model,card,item['path'],job['condition'],target,device=device)
                if result['image_sha256']!=item['sha256']:raise ValueError('推理期间原图变化，拒绝候选')
                row={'index':index,'uri':item['uri'],'state':'completed',
                     'seconds':time.monotonic()-started,'annotations':len(result['annotations']),'skipped':len(result['skipped']),
                     'result_sha256':digest(target/'candidates.json'),
                     'asset_hashes':{name:digest(target/name) for name in
                         ('candidate-mask.png','candidate-overlay.png','candidate-probability.npy')}}
                if 'self_check' in result:
                    row['review_state']=result['self_check']['state']
            except Exception as exc:
                row={'index':index,'uri':item['uri'],'state':'failed','error':str(exc)}
            row.update(device_id=job.get('device_id'),device=device,hardware=job.get('hardware'))
            job.pop('active_started_at',None)
            job['active_index']=None
            previous[index]=row
            job['results']=[previous[k] for k in sorted(previous)]
            job['completed']=len(previous);resource.sample();persist(folder,job)
        failed=sum(r['state']=='failed' for r in job['results'])
        job['state']='failed' if failed==job['total'] else 'completed_with_errors' if failed else 'completed'
        job['message']=f"候选任务完成：{job['total']-failed}张成功，{failed}张失败；原始数据和人工标注未改写"
        persist(folder,job)
    finally:
        del model
        gc.collect()
        if device=='cuda':torch.cuda.empty_cache()


def run_on(folder, job, hardware):
    import torch
    try:
        if hardware['device'] == 'cuda':
            torch.cuda.set_device(hardware['index'])
        job.update(device_id=hardware['id'], hardware=hardware['name'])
        execute(folder, job, hardware['device'])
    except Exception as exc:
        job.update(state='failed',error=str(exc),message='模型任务异常；已完成逐图结果保留，可查询原因')
        persist(folder,job)


def final_test_running(output):
    return any(read(p,{}).get('state') == 'running'
               for p in Path(output).glob('training-*/recognition-test-state.json'))


def run(output):
    from .continuation import lock_alive
    from .solar_device import inventory
    from .solar_forecast import finish
    from .solar_schedule import job_device, ordered_pending

    root = Path(output).resolve() / 'solar-inference'
    root.mkdir(parents=True, exist_ok=True)
    if lock_alive(root/'worker.lock') is True:
        return
    import torch
    devices = inventory()
    torch.set_num_threads(min(torch.get_num_threads(), 4))
    startup = max(0., time.time() - float(os.environ.get('MDVA_WORKER_STARTED', time.time())))
    gpu_lock = ROOT / '.mdva/training-gpu.lock'
    count = sum(d['available'] for d in devices)
    with (exclusive(root/'worker.lock'), ExitStack() as reservation,
          ThreadPoolExecutor(max_workers=count) as compute, ThreadPoolExecutor(max_workers=1) as forecasts):
        atomic_json(root/'devices.json', devices)
        active = {}
        forecast_jobs = {}
        forecast_errors = set()
        reserved = False
        while True:
            for key, (task, job_id) in list(active.items()):
                if task.done():
                    task.result()
                    del active[key]
            for job_id, task in list(forecast_jobs.items()):
                if task.done():
                    if task.exception() is not None:
                        print(f'事件试算结果保存失败 {job_id}: {task.exception()}', flush=True)
                        forecast_errors.add(job_id)
                    del forecast_jobs[job_id]
            if reserved and not any(key.startswith('cuda:') for key in active):
                reservation.close()
                reserved = False

            jobs = [read(p) for p in root.glob('*/job.json')]
            for job in jobs:
                if (job['id'] in forecast_jobs or job['id'] in forecast_errors or job['state'] not in ('completed','completed_with_errors','failed')
                        or not job.get('inputs') or 'photo_id' not in job['inputs'][0]):
                    continue
                folder = root / job['id']
                if any(r['state'] == 'completed' and not (folder/f"image-{r['index']:06d}"/'forecast.json').is_file()
                       for r in job['results']):
                    forecast_jobs[job['id']] = forecasts.submit(finish, output, folder, job)

            pending = ordered_pending(jobs)
            running = {job_id for task, job_id in active.values()}
            for job in pending:
                if job['id'] in running:
                    continue
                folder = root / job['id']
                hardware = job_device(job, devices)
                if hardware is None:
                    job.update(state='failed', error='所选显卡不可用，请使用可用设备重试')
                    persist(folder, job)
                    continue
                key = hardware['id']
                if key in active:
                    continue
                if hardware['device'] == 'cuda' and not reserved:
                    if not final_test_running(output) and lock_alive(gpu_lock) is False:
                        try:
                            reservation.enter_context(exclusive(gpu_lock))
                            reserved = True
                        except (RuntimeError, OSError):
                            pass
                    if not reserved:
                        if job['state'] != 'waiting_gpu':
                            job.update(state='waiting_gpu',message='等待 GPU：训练、测试或其他工作区正在使用显卡')
                            persist(folder,job)
                        continue
                if startup is not None:
                    job['worker_startup_seconds'] = startup
                    startup = None
                active[key] = (compute.submit(run_on, folder, job, hardware), job['id'])

            if not pending and not active and not forecast_jobs:
                return
            time.sleep(.3)


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
