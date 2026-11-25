"""Validate the selected Conda GPU runtime without fitting any model."""
from __future__ import annotations

import argparse
import gc
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import site
import sys
import time


def check(expected_prefix=None):
    prefix = Path(sys.prefix).resolve()
    if expected_prefix and prefix != Path(expected_prefix).resolve():
        raise RuntimeError(f'当前解释器不在指定环境：{prefix}')
    if prefix.name.lower() != 'mdva311' or prefix.parent.name.lower() != 'envs':
        raise RuntimeError('请先 conda activate mdva311；禁止在 base 或系统 Python 中执行模型检查')
    for key, leaf in [('TORCH_HOME','torch'),('HF_HOME','huggingface'),('PIP_CACHE_DIR','pip')]:
        os.environ.setdefault(key, str(prefix/'.cache'/leaf))
        if not Path(os.environ[key]).resolve().is_relative_to(prefix):
            raise RuntimeError(f'{key} 不在选中的 Conda 环境内')
    import numpy as np
    import torch
    import torchvision
    from torchvision.ops import nms
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    names = ['torch','torchvision','numpy','scipy','pandas','scikit-learn','scikit-image',
             'astropy','pillow','openpyxl','opencv-python-headless','matplotlib','safetensors',
             'hydra-core','iopath','albumentations','pytest']
    packages = {}
    for name in names:
        dist = importlib.metadata.distribution(name)
        location = Path(dist.locate_file('')).resolve()
        if not location.is_relative_to(prefix): raise RuntimeError(f'{name} 泄漏到其他环境：{location}')
        packages[name] = {'version': dist.version, 'location': str(location)}
    # pip's declared requirements miss some runtime incompatibilities (e.g. astropy6/numpy2.4).
    for module in ('astropy.units','astropy.visualization','cv2','skimage','pandas','h5py',
                   'pyarrow','matplotlib','openpyxl','albumentations','hydra','iopath'):
        importlib.import_module(module)
    if site.ENABLE_USER_SITE: raise RuntimeError('用户 site-packages 未关闭')
    if not torch.cuda.is_available(): raise RuntimeError('CUDA 不可用')
    torch.cuda.reset_peak_memory_stats()
    torch.manual_seed(20261112)
    start = time.perf_counter()
    checks = {}
    # Real CUDA convolution + loss + gradient under mixed precision.
    network = torch.nn.Sequential(torch.nn.Conv2d(1,32,3,padding=1),torch.nn.ReLU(),
                                  torch.nn.Conv2d(32,1,1)).cuda()
    x = torch.rand(1,1,512,512,device='cuda')
    with torch.autocast('cuda',dtype=torch.float16):
        loss = torch.nn.functional.binary_cross_entropy_with_logits(network(x),torch.zeros_like(x))
    loss.backward();torch.cuda.synchronize()
    assert all(p.grad is not None and torch.isfinite(p.grad).all() for p in network.parameters())
    checks['cuda_convolution_amp_backward'] = True
    boxes=torch.tensor([[0.,0.,10.,10.],[1.,1.,9.,9.],[20.,20.,25.,25.]],device='cuda')
    keep=nms(boxes,torch.tensor([.9,.8,.7],device='cuda'),.5)
    assert keep.tolist()==[0,2];checks['torchvision_cuda_nms']=True
    q=torch.rand(1,2,64,32,device='cuda',requires_grad=True)
    attn=torch.nn.functional.scaled_dot_product_attention(q,q,q)
    attn.sum().backward();assert torch.isfinite(q.grad).all()
    checks['attention_backward']=True
    resnet=torchvision.models.resnet34(weights=None,num_classes=2).cuda().eval()
    with torch.no_grad(): output=resnet(torch.rand(1,3,224,224,device='cuda'))
    assert output.shape==(1,2) and torch.isfinite(output).all()
    checks['resnet34_no_download_forward']=True
    del resnet,output
    gc.collect();torch.cuda.empty_cache()
    flat_source=prefix/'model_sources/flat_unet/fs_model.py'
    if flat_source.is_file():
        import hashlib
        meta=json.loads((flat_source.parent/'PROVENANCE.json').read_text(encoding='utf-8'))
        assert hashlib.sha256(flat_source.read_bytes()).hexdigest()==meta['model_source_sha256']
        spec=importlib.util.spec_from_file_location('mdva_verified_flat_unet',flat_source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        flat=module.Flat_Unet([1,1,1,1,0],flat_channels=32).cuda()
        with torch.autocast('cuda',dtype=torch.float16):
            flat_output=flat(torch.rand(1,1,512,512,device='cuda'))
            flat_loss=torch.nn.functional.binary_cross_entropy_with_logits(flat_output,torch.zeros_like(flat_output))
        flat_loss.backward();torch.cuda.synchronize()
        assert flat_output.shape==(1,1,512,512)
        assert all(torch.isfinite(p.grad).all() for p in flat.parameters() if p.grad is not None)
        checks['flat_unet_32_author_architecture_forward_backward']=True
        del flat,flat_output,flat_loss;gc.collect();torch.cuda.empty_cache()
    from sam2.build_sam import build_sam2
    sam=build_sam2('configs/sam2.1/sam2.1_hiera_t.yaml',ckpt_path=None,device='cuda')
    with torch.no_grad(): embedded=sam.forward_image(torch.zeros(1,3,1024,1024,device='cuda'))
    assert embedded['vision_features'].isfinite().all()
    checks['sam21_tiny_random_init_image_encoder']=True
    packages['SAM-2']={'version':importlib.metadata.version('SAM-2'),
                       'location':str(Path(importlib.import_module('sam2').__file__).resolve())}
    del sam,embedded;gc.collect();torch.cuda.empty_cache()
    prep=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler())
    fitted=prep.fit_transform(np.array([[1.,np.nan],[2.,4.],[3.,6.]]))
    assert np.isfinite(fitted).all();checks['sklearn_numeric_preprocessing']=True
    torch.cuda.synchronize()
    result={'ok':True,'python':sys.version.split()[0],'executable':sys.executable,'prefix':str(prefix),
            'user_site_enabled':site.ENABLE_USER_SITE,'packages':packages,'gpu':torch.cuda.get_device_name(0),
            'capability':list(torch.cuda.get_device_capability(0)),'cuda_runtime':torch.version.cuda,
            'compiled_architectures':torch.cuda.get_arch_list(),
            'peak_allocated_mib':round(torch.cuda.max_memory_allocated()/1024**2,1),
            'checks':checks,'elapsed_seconds':round(time.perf_counter()-start,3),
            'cache_paths':{k:os.environ[k] for k in ('TORCH_HOME','HF_HOME','PIP_CACHE_DIR')},
            'formal_training_started':False,'downloaded_model_weights':False}
    del network,x,loss,boxes,keep,q,attn
    gc.collect();torch.cuda.empty_cache()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-prefix',default='E:/ANACONDA/envs/mdva311')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=check(args.expected_prefix)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
