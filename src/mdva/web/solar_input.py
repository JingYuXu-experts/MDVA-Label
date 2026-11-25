"""Direct image input to local solar inference; no user path/URL or fake dataset."""
from __future__ import annotations

from functools import lru_cache
import base64
from datetime import datetime,timezone
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image,UnidentifiedImageError

from . import solar_models as models
from ..training.durable import commit
from ..training.runtime import digest

MAX_BYTES=4*1024*1024


@lru_cache(maxsize=3)
def _index(rows_path, sources_path, rows_stamp, sources_stamp, tested):
    rows={r['uri']:r for r in _json_lines(rows_path)}
    protected={r['uri'] for r in rows.values() if r['split']=='calibration' or (r['split']=='test' and (r['category']=='FL' or not tested))}
    blocked_pixels={rows[uri].get('pixel_sha256') for uri in protected}
    blocked_bytes=set()
    known={}
    for source in _json_lines(sources_path):
        row=rows.get(source['uri'])
        if not row:continue
        if row['uri'] in protected:
            blocked_bytes.add(source['sha256'])
        elif row.get('eligible',True):
            known[source['sha256']]={k:row.get(k) for k in ('category','time_utc','instrument','uri')}
    return blocked_bytes,blocked_pixels,known


def _json_lines(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():yield json.loads(line)


def input_identity(exp, image_hash, pixel_hashes):
    dataset=Path(models.read(exp/'protocol.json')['dataset'])
    rows=dataset/'segmentation-ready.jsonl'
    sources=dataset/'source-files.jsonl'
    tested=models.read(exp/'recognition-test-state.json',{}).get('state')=='completed'
    blocked,pixels,known=_index(rows,sources,rows.stat().st_mtime_ns,sources.stat().st_mtime_ns,tested)
    if image_hash in blocked or pixel_hashes & pixels:
        raise ValueError('图片与封存的测试/校准样本相同，不能通过上传或重编码绕过')
    return known.get(image_hash,{})


def decode_image(params):
    name=str(params.get('filename','图像'))
    if len(name)>200 or re.search(r'[\x00-\x1f/\\:]',name):raise ValueError('文件名不合法，不接受路径')
    data=params.get('image_data')
    if not isinstance(data,str) or len(data)>MAX_BYTES*4//3+100:raise ValueError('图像不得超过4MiB')
    match=re.fullmatch(r'data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)',data)
    if not match:raise ValueError('请上传JPG/PNG图片；不接受网址、FITS或本地路径')
    try:raw=base64.b64decode(match[2],validate=True)
    except ValueError:raise ValueError('图片编码损坏') from None
    if len(raw)>MAX_BYTES:raise ValueError('图像不得超过4MiB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width,height=image.size
            if min(width,height)<256 or width*height>32_000_000 or not .9<=width/height<=1.1:
                raise ValueError('当前模型需要至少256像素、近方形的完整日面图像，不支持局部截图')
            if image.format!={'png':'PNG','jpeg':'JPEG'}[match[1]] or getattr(image,'n_frames',1)!=1:
                raise ValueError('图片格式不符或包含多帧')
            if image.mode not in ('L','RGB','RGBA'):raise ValueError('只接受8位显示图；原始数值图需单独适配')
            if image.getexif().get(274,1)!=1:raise ValueError('图片含显示旋转标记，请先规范方向以保证范围坐标一致')
            if image.mode=='RGBA' and image.getextrema()[3]!=(255,255):raise ValueError('请提供不透明的完整观测图')
            image.load()
            pixel_hashes={hashlib.sha256(f'{mode}:{image.size}:'.encode()+image.convert(mode).tobytes()).hexdigest()
                          for mode in {'L','RGB',image.mode}}
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
        raise ValueError('图片损坏或像素规模异常') from None
    return name,raw,width,height,pixel_hashes,('.png' if match[1]=='png' else '.jpg')


def analyze(hub,output,params,*,spawn=True):
    identifier=str(params.get('model_id',''))
    model=next((m for m in models.inventory(output)['models'] if m['id']==identifier),None)
    if not model or not model['available']:raise ValueError((model or {}).get('reason') or '模型不可用')
    if not model.get('scientific_test_complete') and params.get('acknowledge_research') is not True:
        raise ValueError('尚未完成独立测试的模型只允许明确的研究试用')
    if params.get('condition')!=model['condition'] or params.get('confirm_input') is not True:
        raise ValueError('请确认完整日面图像的仪器/通道与模型输入一致')
    request_id=str(params.get('request_id',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):raise ValueError('缺少合法请求ID')
    name,raw,width,height,pixel_hashes,suffix=decode_image(params)
    observed=str(params.get('observed_utc','')).strip()
    if observed:
        try:
            time=datetime.fromisoformat(observed.replace('Z','+00:00'))
            if time.tzinfo is None:raise ValueError
            observed=time.astimezone(timezone.utc).isoformat()
        except ValueError:raise ValueError('观测时间须包含时区；未知可留空') from None
    exp=models.experiment(output)
    image_hash=hashlib.sha256(raw).hexdigest()
    input_identity(exp,image_hash,pixel_hashes)
    folder=Path(output).resolve()/'solar-inputs'/models.workspace_key(hub)
    source=hub.store.require_writable(folder/(image_hash+suffix))
    provenance={'filename':name,'width':width,'height':height,'sha256':image_hash,
        'condition':model['condition'],'condition_basis':'user_confirmed','observed_utc':observed or None,
        'time_basis':'user_supplied' if observed else 'unknown','reference_mask_input':False}
    with models._lock:
        for job_file in models.job_root(output).glob('*/job.json'):
            old=models.read(job_file)
            if old['workspace']==models.workspace_key(hub) and old['request_id']==request_id:
                if old.get('input_metadata')!=provenance or old['model_id']!=identifier:
                    raise ValueError('请求ID已用于不同输入；请创建新任务')
        if source.exists():
            if digest(source)!=image_hash:raise ValueError('已保存的输入身份异常')
        else:commit(source,lambda stream:stream.write(raw))
        return models.enqueue(hub,output,model,exp,[{'uri':hub.store.to_rel(source),'path':str(source),'sha256':image_hash}],
                              '', '',request_id,spawn=spawn,provenance=provenance)


def original(hub,output,identifier,index=0):
    _,job=models._job(output,identifier,hub)
    if not 0<=index<len(job['inputs']):raise ValueError('图像序号不合法')
    row=job['inputs'][index];path=hub.store.require_within(row['uri'])
    if digest(path)!=row['sha256']:raise ValueError('输入图像已变化')
    return path.read_bytes(),'image/png' if path.suffix.lower()=='.png' else 'image/jpeg'
