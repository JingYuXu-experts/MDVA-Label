"""Lossless uint8 preprocessing cache, native labels retained at read-only sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import numpy as np

from .datasets import SegmentationDataset as OriginalDataset
from .durable import atomic_json
from .runtime import digest, output_path, now

ROOT=Path(__file__).resolve().parents[3]
DEFAULT_CACHE=ROOT/'out/training-cache/20261116-v1'
ALLOWED_SPLITS=('train','validation')


def cache_folder(root,category,split,size):
    if category not in ('AR','CH') or split not in ALLOWED_SPLITS or size!=512:
        raise ValueError('缓存仅允许AR/CH的512训练及验证；校准/测试禁止')
    return Path(root)/f'{category}-{split}-{size}'


def identity(directory,rows):
    from .reviewed_train import data_identity
    return {'data':data_identity(directory),'fingerprints':digest(Path(directory)/'source-files.jsonl'),
            'ordered_rows':hashlib.sha256(json.dumps([(r['uri'],r['mask_uri']) for r in rows],ensure_ascii=False).encode()).hexdigest()}


def build(directory,root,category,split,size=512):
    import torch
    directory=Path(directory).resolve();root=output_path(root,directory)
    target=cache_folder(root,category,split,size)
    data=OriginalDataset(directory,category,split,size)
    expected=identity(directory,data.rows)
    if (target/'manifest.json').exists():
        old=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
        if old['identity']!=expected:raise ValueError('缓存来源改变，建立新缓存版本')
        for name,h in old['files'].items():
            if digest(target/name)!=h:raise ValueError('已有缓存损坏，禁止复用')
        return old
    if target.exists():raise ValueError('缓存目录无完整清单，请保留失败证据并换新版本')
    root.mkdir(parents=True,exist_ok=True)
    pending=root/(target.name+'.building-'+uuid.uuid4().hex);pending.mkdir()
    images=np.lib.format.open_memmap(pending/'images.npy',mode='w+',dtype='uint8',shape=(len(data),size,size))
    masks=np.lib.format.open_memmap(pending/'masks.npy',mode='w+',dtype='uint8',shape=(len(data),size,size))
    started=time.perf_counter()
    for i,row in enumerate(data.rows):
        item=data[i]
        source_image=item['image'].numpy()[0];source_mask=item['semantic_mask'].numpy()[0]
        image=np.rint(source_image*255).astype('uint8');mask=source_mask.astype('uint8')
        if not np.array_equal(image.astype('float32')/255.,source_image) or not np.array_equal(mask.astype('float32'),source_mask):
            raise ValueError('缓存量化不能逐像素无损还原：'+row['uri'])
        images[i]=image;masks[i]=mask
        if i%200==0:
            atomic_json(root/'build-progress.json',{'state':'building','category':category,'split':split,'processed':i+1,
                'total':len(data),'updated_utc':now(),'lossless_verified':True})
            print(json.dumps({'category':category,'split':split,'processed':i+1,'total':len(data)}),flush=True)
    images.flush();masks.flush();del images,masks
    value={'schema':1,'category':category,'split':split,'size':size,'count':len(data),'identity':expected,
        'files':{name:digest(pending/name) for name in ('images.npy','masks.npy')},
        'rows':[(r['uri'],r['mask_uri']) for r in data.rows],'pixelwise_equal_to_original':True,
        'source_fingerprints_verified':True,'build_seconds':time.perf_counter()-started,'created_utc':now()}
    atomic_json(pending/'manifest.json',value)
    os.replace(pending,target)
    return value


class CachedDataset(OriginalDataset):
    def __init__(self,directory,category,split,size=None,*,cache_root=DEFAULT_CACHE):
        if split not in ALLOWED_SPLITS:raise ValueError('缓存禁止校准/测试输入')
        super().__init__(directory,category,split,size)
        self.cache=cache_folder(cache_root,category,split,size).resolve()
        manifest=json.loads((self.cache/'manifest.json').read_text(encoding='utf-8'))
        if manifest['identity']!=identity(directory,self.rows) or manifest['count']!=len(self.rows):
            raise ValueError('缓存与冻结清单身份不同')
        for name,h in manifest['files'].items():
            if digest(self.cache/name)!=h:raise ValueError('预处理缓存内容已改变')
        # Keep the original per-session raw SHA checks; caching must not hide modified originals.
        for row in self.rows:
            self.source(row['uri']);self.source(row['mask_uri'])
        self.positions={tuple(pair):i for i,pair in enumerate(manifest['rows'])}
        self._images=None;self._masks=None

    def __getstate__(self):
        state=self.__dict__.copy();state['_images']=None;state['_masks']=None
        return state

    def __getitem__(self,index):
        import torch
        if self._images is None:
            self._images=np.load(self.cache/'images.npy',mmap_mode='r',allow_pickle=False)
            self._masks=np.load(self.cache/'masks.npy',mmap_mode='r',allow_pickle=False)
        row=self.rows[index];position=self.positions[(row['uri'],row['mask_uri'])]
        image=self._images[position].astype('float32')/255.
        mask=self._masks[position].astype('float32')
        # The training loop never uses instance_mask. Native instance IDs remain in the source file.
        return {'image':torch.from_numpy(image[None]),'semantic_mask':torch.from_numpy(mask[None]),
                'uri':row['uri'],'category':row['category'],'split':row['split']}


def worker_initialize(_):
    import torch
    torch.set_num_threads(1)


def loader_for(dataset,config,epoch,micro_batch):
    import torch
    from torch.utils.data import DataLoader,WeightedRandomSampler
    from collections import Counter
    generator=torch.Generator().manual_seed(config['seed']+epoch*100003)
    sampler=None
    if config.get('station_balance'):
        counts=Counter(r['instrument'] for r in dataset.rows)
        sampler=WeightedRandomSampler([1/counts[r['instrument']] for r in dataset.rows],len(dataset),
            replacement=True,generator=generator)
    workers=config.get('data_workers',0)
    if workers not in (0,2,4):raise ValueError('未登记的数据读取并发数')
    kwargs={'multiprocessing_context':'spawn','prefetch_factor':2,'worker_init_fn':worker_initialize} if workers else {}
    return DataLoader(dataset,batch_size=micro_batch,shuffle=sampler is None,sampler=sampler,num_workers=workers,
                      generator=generator,pin_memory=torch.cuda.is_available(),**kwargs)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--cache',type=Path,default=DEFAULT_CACHE)
    args=p.parse_args()
    for category in ('AR','CH'):
        for split in ALLOWED_SPLITS:build(args.dataset,args.cache,category,split)
    atomic_json(args.cache/'build-progress.json',{'state':'completed','updated_utc':now(),'test_accessed':False})
