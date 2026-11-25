"""Local solar candidate jobs. No torch import in the HTTP process."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import sys
import threading
import uuid

from ..training.durable import atomic_json
from ..training.runtime import digest, now

ROOT=Path(__file__).resolve().parents[3]
MODELS={'AR-A0-s42':('AR','magnetogram'),'CH-C2-s2026':('CH','AIA193')}
_lock=threading.RLock()
_workers={}


def read(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default


def experiment(output):
    from .training import experiment_folder
    return experiment_folder(ROOT,output)


def model_python():
    current=Path(sys.prefix)
    prefix=current if current.name.lower()=='mdva311' else (current.parent if current.parent.name.lower()=='envs' else current/'envs')/'mdva311'
    binary=Path(os.environ.get('MDVA_MODEL_PYTHON',str(prefix/('python.exe' if os.name=='nt' else 'bin/python')))).resolve()
    envprefix=binary.parent if os.name=='nt' else binary.parent.parent
    if envprefix.name.lower()!='mdva311' or envprefix.parent.name.lower()!='envs':
        raise ValueError('模型运行环境必须为Conda envs/mdva311')
    return binary,envprefix


def inventory(output):
    try:exp=experiment(output)
    except FileNotFoundError:return {'models':[],'notice':'尚无本地模型实验'}
    try:binary,_=model_python();env_ok=binary.is_file()
    except ValueError:env_ok=False
    frozen=read(exp/'recognition-evaluation-protocol.json',{})
    models=[]
    for identifier,(category,condition) in MODELS.items():
        package=exp/'packages'/identifier;card=read(package/'model.json')
        reason=''
        if not card:reason='模型包尚未生成'
        elif not env_ok:reason='未找到Conda mdva311，请检查首次环境设置'
        else:
            if card.get('id')!=identifier or card.get('category')!=category or card.get('input_condition')!=condition:
                reason='模型包类别或输入条件不符'
            elif not (package/'weights.safetensors').is_file():reason='缺少权重文件'
            elif digest(package/'weights.safetensors')!=card['weights_sha256']:reason='权重校验失败'
            elif card['config']!=frozen.get('recognition',{}).get(identifier,{}).get('configuration'):
                reason='模型包不属于当前冻结候选'
            else:
                for name,value in card.get('training_code',{}).items():
                    if Path(name).name!=name or not name.endswith('.py') or not (ROOT/'src/mdva/training'/name).is_file() or digest(ROOT/'src/mdva/training'/name)!=value:
                        reason='训练与推理代码身份不一致';break
        entry=frozen.get('recognition',{}).get(identifier,{})
        test=read(exp/entry.get('run_directory','missing')/'evaluation/test/summary.json')
        tested=bool(test and test.get('checkpoint_sha256')==entry.get('checkpoint_sha256'))
        models.append({'id':identifier,'category':category,'condition':condition,'available':not reason,'reason':reason,
            'release_state':'research_candidate','scientific_test_complete':tested,
            'test_state':'completed' if tested else 'awaiting_confirmation',
            'validation':(card or {}).get('validation'), 'test':test if tested else None,
            'weights_sha256':(card or {}).get('weights_sha256'),
            'input':(card or {}).get('input'),'score_scope':'区域平均模型分数，未校准；候选须人工核对'})
    return {'models':models,'training_stopped':True,'notice':'候选仅用于AR/CH区域辅助标注，不产生NOAA身份或空间天气预报'}


def job_root(output):return Path(output).resolve()/'solar-inference'


def workspace_key(hub):return hashlib.sha256(str(hub.path.resolve()).casefold().encode()).hexdigest()[:24]


def _job(output,identifier,hub):
    if not re.fullmatch(r'[a-f0-9]{32}',str(identifier)):raise ValueError('任务ID不合法')
    folder=job_root(output)/identifier;value=read(folder/'job.json')
    if not value or value['workspace']!=workspace_key(hub):raise FileNotFoundError('当前工作区没有此任务')
    return folder,value


def ensure_worker(output, *, recover_forecasts=False):
    root=job_root(output)
    if not root.exists():return
    pending=any(read(p,{}).get('state') in ('queued','waiting_gpu','running','interrupted') for p in root.glob('*/job.json'))
    if not (pending or recover_forecasts):return
    from ..training.continuation import lock_alive
    with _lock:
        if lock_alive(root/'worker.lock') is True:return
        existing=_workers.get(str(root))
        if existing and existing.poll() is None:return
        binary,prefix=model_python()
        if not binary.is_file():return
        env={**os.environ,**read(prefix/'conda-meta/state',{}).get('env_vars',{}),
             'PYTHONIOENCODING':'utf-8','PYTHONNOUSERSITE':'1','MDVA_WORKER_STARTED':str(time.time())}
        with (root/'worker-console.log').open('a',encoding='utf-8') as stream:
            process=subprocess.Popen([str(binary),'-u','-m','mdva.training.solar_worker','--output',str(Path(output).resolve())],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
        _workers[str(root)]=process


def create(hub,output,params,*,spawn=True):
    identifier=str(params.get('model_id',''))
    model=next((m for m in inventory(output)['models'] if m['id']==identifier),None)
    if not model or not model['available']:raise ValueError((model or {}).get('reason') or '模型未登记或不可用')
    if params.get('condition')!=model['condition']:raise ValueError('请明确选择与模型匹配的观测条件')
    if not model['scientific_test_complete'] and params.get('acknowledge_research') is not True:
        raise ValueError('模型尚未完成独立测试；研究试用需明确确认仅作为人工候选')
    uris=params.get('uris');dataset=str(params.get('dataset',''));version=str(params.get('version',''))
    if not isinstance(uris,list) or not 1<=len(uris)<=200 or any(not isinstance(u,str) or not u.strip() for u in uris):
        raise ValueError('每次请选择1—200张已登记图像')
    if len(set(uris))!=len(uris):raise ValueError('图像列表重复')
    ds=hub.store.get_dataset(dataset);ver=hub.store.get_version(version)
    if not ds or not ver or ver['dataset_id']!=dataset:raise ValueError('数据集与版本不匹配')
    if (ds.get('meta') or {}).get('indexed'):
        registered={r['uri']:r for r in hub.store.version_records(version)}
    else:registered={r['uri']:r for r in hub.items(dataset,version)}
    exp=experiment(output);protocol=read(exp/'protocol.json')
    rows=[json.loads(line) for line in (Path(protocol['dataset'])/'segmentation-ready.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    tests_complete=read(exp/'recognition-test-state.json',{}).get('state')=='completed'
    protected={r['uri'] for r in rows if r['split']=='calibration' or
               (r['split']=='test' and (r['category']=='FL' or not tests_complete))}
    fingerprints=[json.loads(line) for line in (Path(protocol['dataset'])/'source-files.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    protected_hashes={r['sha256'] for r in fingerprints if r['uri'] in protected}
    inputs=[]
    for uri in uris:
        rel=hub.store.to_rel(uri)
        if rel not in registered:raise ValueError('所选图像不属于该数据集版本')
        if rel in protected:raise ValueError('该图像属于封存的测试/校准范围，不能用于候选试用')
        category=registered[rel].get('category')
        if category and category!=model['category']:raise ValueError('批量图像类别与所选模型不匹配')
        source=hub.store.require_within(rel)
        if source.suffix.lower() not in ('.jpg','.jpeg','.png') or not source.is_file():
            raise ValueError('首轮模型只接受已登记的显示域JPG/PNG图像')
        image_hash=digest(source)
        if image_hash in protected_hashes:raise ValueError('图像与封存样本同源，不能通过副本用于候选试用')
        inputs.append({'uri':rel,'path':str(source),'sha256':image_hash})
    return enqueue(hub,output,model,exp,inputs,dataset,version,str(params.get('request_id','')),spawn=spawn)


def enqueue(hub,output,model,exp,inputs,dataset,version,request_id,*,spawn=True,provenance=None,target_device=None):
    """Internal enqueue after dataset-membership or uploaded-image validation."""
    identifier=model['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):raise ValueError('缺少合法幂等请求ID')
    identity={'model':identifier,'weights':model['weights_sha256'],'dataset':dataset,
              'version':version,'inputs':inputs,'condition':model['condition']}
    if provenance is not None:identity['provenance']=provenance
    if target_device is not None:identity['target_device']=target_device
    signature=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    root=job_root(output);root.mkdir(parents=True,exist_ok=True)
    with _lock:
        for path in root.glob('*/job.json'):
            old=read(path)
            if old['workspace']==workspace_key(hub) and old['request_id']==request_id:
                if old['signature']!=signature:raise ValueError('请求ID已用于不同输入；请创建新任务')
                if spawn:ensure_worker(output)
                return public(old)
        job_id=uuid.uuid4().hex
        task=hub.store.create_task('solar_inference',dataset_id=dataset or None,version_in=version or None,
                                   params={'job_id':job_id,'model_id':identifier,'n_images':len(inputs)})
        job={'id':job_id,'workspace':workspace_key(hub),'workspace_db':str(hub.path.resolve()),'task_id':task,
             'request_id':request_id,'signature':signature,'model_id':identifier,'weights_sha256':model['weights_sha256'],
             'package':str(exp/'packages'/identifier),'dataset':dataset,'version':version,
             'condition':model['condition'],'inputs':inputs,'state':'queued','created_utc':now(),'updated_utc':now(),
             'completed':0,'total':len(inputs),'results':[],'message':'任务已登记，等待独立模型进程'}
        if provenance:job.update(input_kind='uploaded_image',input_metadata=provenance)
        if target_device is not None:job['target_device']=target_device
        atomic_json(root/job_id/'job.json',job)
        if spawn:ensure_worker(output)
        return public(job)


def public(value):
    return {k:v for k,v in value.items() if k in ('id','model_id','task_id','dataset','version','condition',
        'state','created_utc','updated_utc','completed','total','results','message','error','input_kind','input_metadata')}


def jobs(hub,output,identifier=''):
    ensure_worker(output)
    if identifier:return public(_job(output,identifier,hub)[1])
    rows=[read(p) for p in job_root(output).glob('*/job.json')]
    rows=[public(r) for r in rows if r['workspace']==workspace_key(hub)]
    rows.sort(key=lambda r:r['created_utc'],reverse=True)
    return {'jobs':rows[:30]}


def result(hub,output,identifier,index,*,include_review=True):
    folder,job=_job(output,identifier,hub)
    if not 0<=index<len(job['inputs']):raise ValueError('图像序号不合法')
    row=next((r for r in job['results'] if r['index']==index and r['state']=='completed'),None)
    if not row:raise FileNotFoundError('此图像尚无完整候选')
    item=job['inputs'][index]
    if digest(hub.store.require_within(item['uri']))!=item['sha256']:raise ValueError('原图已变化；旧候选不得接纳，请新建任务')
    path=folder/f'image-{index:06d}'/'candidates.json'
    if digest(path)!=row['result_sha256']:raise ValueError('候选记录已变化')
    value=read(path)
    if value['image_sha256']!=item['sha256']:raise ValueError('候选原图身份不符')
    if include_review and job.get('input_kind')=='uploaded_image' and job['model_id']=='CH-C2-s2026':
        from .solar_review import result_evidence
        card=Path(job['package'])/'model.json'
        mask=path.with_name('candidate-mask.png')
        def read_mask():
            if not mask.resolve().is_relative_to(folder.resolve()):raise ValueError('候选掩膜路径越界')
            return mask.read_bytes()
        value['physical_review']=result_evidence(output,job,value,card,read_mask)
    return {k:v for k,v in value.items() if k!='image'} | {'uri':item['uri'],'index':index,
        'overlay_id':f'image-{index:06d}/candidate-overlay.png','mask_id':f'image-{index:06d}/candidate-mask.png'}


def asset(hub,output,identifier,asset_id):
    folder,job=_job(output,identifier,hub)
    match=re.fullmatch(r'image-(\d{6})/(candidate-overlay\.png|candidate-mask\.png|candidate-probability\.npy)',asset_id)
    if not match:raise ValueError('资源ID不合法，不接受路径或网址')
    index=int(match[1]);result(hub,output,identifier,index,include_review=False)
    path=(folder/asset_id).resolve()
    if not path.is_relative_to(folder.resolve()):raise ValueError('资源越界')
    record=next(r for r in job['results'] if r['index']==index)
    if digest(path)!=record['asset_hashes'].get(path.name):raise ValueError('候选资源校验失败')
    return path.read_bytes(),'image/png' if path.suffix=='.png' else 'application/octet-stream'


def annotations_archive(hub,output,identifier):
    """Export the automatic results directly, without modifying reviewed annotations."""
    import io
    import zipfile
    import numpy as np
    from PIL import Image
    from .solar_products import regions
    from ..training.durable import commit
    folder,job=_job(output,identifier,hub)
    if job['state'] not in ('completed','completed_with_errors'):
        raise ValueError('请等待识别任务结束后下载机器标注包')
    manifest={'schema':1,'job_id':identifier,'model_id':job['model_id'],
        'weights_sha256':job['weights_sha256'],'dataset':job['dataset'],'version':job['version'],
        'condition':job['condition'],'status':'machine_unreviewed','threshold':.5,
        'region_semantics':'8连通像素区域，不等于独立物理实例或NOAA活动区身份',
        'measurement_scope':'原图像素坐标与面积，不是太阳表面物理面积；未进行投影校正',
        'forecast':{'state':'not_computed','reason':'区域形状本身不足以推出耀斑、CME或地球影响概率'},
        'images':[],'failures':[r for r in job['results'] if r['state']!='completed']}
    category=MODELS[job['model_id']][0]
    coco={'info':{'description':'MDVA machine unreviewed semantic components; not physical instances'},
          'images':[],'annotations':[],'categories':[{'id':1,'name':category}]}
    # Verify all registered source/result/resource hashes before writing any archive.
    records=[]
    for row in job['results']:
        if row['state']!='completed':continue
        value=result(hub,output,identifier,row['index'])
        data,_=asset(hub,output,identifier,value['mask_id'])
        with Image.open(io.BytesIO(data)) as im:
            mask=np.asarray(im)
        if mask.shape!=(value['height'],value['width']):raise ValueError('掩膜尺寸与原图不一致')
        records.append((row,value))
    if not records:raise ValueError('没有可导出的成功识别结果')
    def produce(stream):
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for row,value in records:
                data,_=asset(hub,output,identifier,value['mask_id'])
                with Image.open(io.BytesIO(data)) as im:mask=np.asarray(im)
                image_id=row['index']+1
                name=f'masks/{image_id:06d}.png'
                source=hub.store.require_within(value['uri'])
                original=source.read_bytes()
                if hashlib.sha256(original).hexdigest()!=value['image_sha256']:
                    raise ValueError('整理标注包时原图变化，拒绝导出')
                image_name=f'images/{image_id:06d}{source.suffix.lower()}'
                components=regions(mask)
                manifest['images'].append({'image_id':image_id,'uri':value['uri'],
                    'image_sha256':value['image_sha256'],'width':value['width'],'height':value['height'],
                    'image':image_name,'mask':name,'mask_sha256':row['asset_hashes']['candidate-mask.png'],
                    'foreground_pixels':int((mask>0).sum()),'regions':components,
                    'polygon_conversion_skips':value['skipped'],
                    'self_check':value.get('self_check',{'state':'unavailable','reason':'此结果生成时尚未执行自检'})})
                if 'physical_review' in value:
                    manifest['images'][-1]['physical_review']=value['physical_review']
                coco['images'].append({'id':image_id,'file_name':image_name,
                    'width':value['width'],'height':value['height']})
                for region in components:
                    # COCO RLE uses iscrowd=1; this is a format flag, not a physical crowd claim.
                    coco['annotations'].append({'id':len(coco['annotations'])+1,'image_id':image_id,
                        'category_id':1,'segmentation':region['segmentation'],'bbox':region['bbox'],
                        'area':region['area_pixels'],'iscrowd':1,'machine_unreviewed':True})
                archive.writestr(name,data)
                archive.writestr(image_name,original)
                archive.writestr(f'polygons/{image_id:06d}.json',json.dumps(value['annotations'],ensure_ascii=False))
            for name,value in [('manifest.json',manifest),('coco.json',coco)]:
                archive.writestr(name,json.dumps(value,ensure_ascii=False,allow_nan=False))
            archive.writestr('README.txt',
                '机器自动标注，未经人工审核。images保留输入原图，COCO使用包内相对路径。\n'
                '掩膜和COCO RLE保留孔洞及微小区域。\n'
                'polygons仅含可转换轮廓，不能代替完整掩膜。连通区域不是物理实例。\n'
                '面积为图像像素面积，不是物理面积。本包不包含空间天气事件预测。\n')
    # Per-job serialization and atomic replacement keep concurrent downloads complete.
    with _lock:
        path=folder/'machine-annotations.zip'
        commit(path,produce)
        return path.read_bytes()
