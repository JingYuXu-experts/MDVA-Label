"""Load only eligible frozen splits; preserve native instance masks and provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .prepare import NUMERIC

# Explicit allowlist: no identity, future label, unverified FI, or later SRS fields.
FEATURES = tuple(NUMERIC) + tuple(k+'_missing' for k in NUMERIC)
SPLITS = ('train', 'validation', 'calibration', 'test')


def read_frozen(directory, name):
    directory = Path(directory)
    hashes = json.loads((directory/'manifest-hashes.json').read_text(encoding='utf-8'))
    path = directory/name
    raw = path.read_bytes()
    if hashes[name] != hashlib.sha256(raw).hexdigest():
        raise ValueError('清单已改变，不能继续使用原来的冻结划分')
    return [json.loads(line) for line in raw.decode('utf-8').splitlines() if line]


def resolve_uri(uri, sources):
    if not uri.startswith('@') or '/' not in uri:
        raise ValueError('仅接受登记数据源 URI')
    source, relative = uri[1:].split('/', 1)
    if source not in sources: raise ValueError('未知数据源')
    base = Path(sources[source]).resolve()
    path = (base/relative).resolve()
    if not path.is_relative_to(base) or not path.is_file(): raise ValueError('文件越界或不存在')
    return path


class SegmentationDataset:
    """Returns image, semantic mask, native instance IDs, and source metadata.

    A 0..1 grayscale tensor is a display-domain ML input, not a calibrated field.
    No augmentation is performed here; only the training split may add it later.
    """
    def __init__(self, directory, category, split, size=None):
        if split not in SPLITS or category not in ('AR','CH','FL'): raise ValueError('类别或划分非法')
        self.sources=json.loads((Path(directory)/'sources.json').read_text(encoding='utf-8'))
        self.fingerprints={r['uri']:r for r in read_frozen(directory,'source-files.jsonl')}
        self.verified=set()
        self.rows=[r for r in read_frozen(directory,'segmentation-ready.jsonl')
                   if r['eligible'] and r['category']==category and r['split']==split]
        self.size = size

    def __len__(self): return len(self.rows)

    def source(self,uri):
        path=resolve_uri(uri,self.sources)
        expected=self.fingerprints[uri]
        stat=path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(expected['bytes'],expected['mtime_ns']):
            raise ValueError('源文件已改变，请重新进行数据准备')
        if uri not in self.verified:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected['sha256']:
                raise ValueError('源文件内容不符合冻结指纹')
            self.verified.add(uri)
        return path

    def __getitem__(self,index):
        import torch
        row=self.rows[index]
        with Image.open(self.source(row['uri'])) as im:
            image=im.convert('L')
            if self.size: image=image.resize((self.size,self.size),Image.Resampling.BILINEAR)
            image=np.asarray(image,dtype=np.float32).copy()/255.
        mask=np.load(self.source(row['mask_uri']),allow_pickle=False)
        if self.size:
            mask=np.asarray(Image.fromarray(mask.astype(np.int32)).resize((self.size,self.size),Image.Resampling.NEAREST)).copy()
        if image.shape != mask.shape: raise ValueError('图像与标签尺寸不一致')
        return {'image':torch.from_numpy(image[None]),
                'semantic_mask':torch.from_numpy((mask>0).astype(np.float32)[None]),
                'instance_mask':torch.from_numpy(mask.astype(np.int64)),
                'uri':row['uri'],'category':row['category'],'split':row['split']}


def forecast_matrix(directory, split):
    if split not in SPLITS: raise ValueError('划分非法')
    rows=[r for r in read_frozen(directory,'forecast-ready.jsonl') if r['split']==split and r['eligible']]
    x=np.array([[r['features'].get(k) if r['features'].get(k) is not None else np.nan for k in FEATURES] for r in rows],dtype=np.float64)
    if not rows: x=np.empty((0,len(FEATURES)))
    return x,np.array([r['target_m_plus'] for r in rows],dtype=np.int64),rows


def freeze_feature_preprocessing(directory):
    """Only fit numeric input transformations on train; never fit a classifier."""
    directory=Path(directory)
    x,_,rows=forecast_matrix(directory,'train')
    if not rows: raise ValueError('没有可用训练记录')
    medians=np.array([np.nanmedian(x[:,i]) if np.isfinite(x[:,i]).any() else 0. for i in range(x.shape[1])])
    filled=np.where(np.isnan(x),medians,x)
    mean,scale=filled.mean(axis=0),filled.std(axis=0)
    scale[scale==0]=1.
    config={'features':list(FEATURES),'fit_split':'train','fit_rows':len(rows),
            'medians':medians.tolist(),'mean':mean.tolist(),'scale':scale.tolist(),
            'noaa_identity_used_as_feature':False,'provided_FI_used_as_feature':False,
            'Hale_McIntosh_used_as_feature':False,'classifier_trained':False}
    (directory/'feature-preprocessing.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    return config
