"""Inference from a locally verified safetensors package, producing review candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .runtime import Resources, digest, exclusive, now, output_path
from .durable import atomic_json


def polygons(probability, category, identifier, threshold=.5):
    import cv2
    if not np.isfinite(probability).all():
        raise ValueError('概率包含非有限值，不能转换为背景或多边形')
    binary = np.asarray(probability >= threshold, dtype=np.uint8)
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    output, skipped = [], []
    if hierarchy is None:
        return output, skipped
    for index, contour in enumerate(contours):
        if hierarchy[0, index, 3] >= 0:
            continue
        # Preserve the raster candidate separately. Holes cannot be represented by one simple poly.
        if hierarchy[0, index, 2] >= 0:
            skipped.append({'component': index, 'reason': '区域含孔洞，当前简单多边形会丢失拓扑，请参照候选掩膜人工处理'})
            continue
        simplified = cv2.approxPolyDP(contour, .5, True).reshape(-1, 2)
        if len(simplified) < 3:
            skipped.append({'component': index, 'reason': '少于3个有效顶点，未生成伪多边形'})
            continue
        fill = np.zeros_like(binary)
        cv2.drawContours(fill, [contour], -1, 1, -1)
        output.append({'geom': 'poly', 'coords': simplified.flatten().astype(float).tolist(),
                       'label': category, 'source': 'preannotate', 'backend': identifier,
                       'confidence': float(np.asarray(probability)[fill > 0].mean()),
                       'note': '模型候选轮廓近似，需人工复核；不是原始真值，也不赋予NOAA身份'})
    return output, skipped


def load_model(package, *, device='cuda'):
    import torch
    from safetensors.torch import load_file
    from .run_locations import model_and_identity
    package=Path(package).resolve()
    card = json.loads((package/'model.json').read_text(encoding='utf-8'))
    architecture, identity = model_and_identity(card['config'])
    if identity()!=card['training_code'] or card['threshold']!=.5:
        raise ValueError('模型包代码或阈值身份不符')
    if digest(package/'weights.safetensors') != card['weights_sha256']:
        raise ValueError('模型权重哈希不符')
    weights=load_file(str(package/'weights.safetensors'))
    if not all(torch.isfinite(t).all() for t in weights.values()):
        raise ValueError('模型包参数非有限')
    model,_=architecture(card['config']);model.load_state_dict(weights)
    if device=='cuda':
        torch.cuda.set_per_process_memory_fraction(.75)
    return model.to(device).eval(),card


def predict_loaded(model,card,image,condition,output,*,device='cuda'):
    import torch
    from PIL import Image
    from .solar_self_check import EXPOSURE_GAIN, assess_stability
    image,output=Path(image).resolve(),Path(output).resolve()
    expected = {'AR': 'magnetogram', 'CH': 'AIA193', 'FL': 'Halpha'}[card['category']]
    if condition != expected:
        raise ValueError('观测条件与模型任务不符，不能仅凭文件像素自动认定通道')
    if output.exists() and any(output.iterdir()):
        raise ValueError('候选输出目录非空，禁止覆盖既有人工审阅结果')
    resources=Resources(device);resources.sample()
    input_sha=digest(image)
    with Image.open(image) as im:
        size=im.size
        if image.suffix.lower() not in ('.jpg','.jpeg','.png') or size[0]*size[1]>32_000_000:
            raise ValueError('首轮仅支持不超过3200万像素的显示域JPG/PNG，FITS需单独适配')
        x=np.asarray(im.convert('L').resize((card['config']['size'],)*2,Image.Resampling.BILINEAR),dtype=np.float32).copy()/255.
    def infer(values):
        with torch.inference_mode(), torch.autocast(device,dtype=torch.float16,enabled=device=='cuda'):
            logits=model(torch.from_numpy(values[None,None]).to(device))
        if not torch.isfinite(logits).all():
            raise ValueError('推理输出非有限，未将其当作背景')
        return torch.nn.functional.interpolate(logits.float().sigmoid(),(size[1],size[0]),mode='bilinear',align_corners=False)[0,0].cpu().numpy()

    p=infer(x)
    candidates,skipped=polygons(p,card['category'],card['id'])
    # Retain the production candidate exactly as predicted by the first pass.
    check=assess_stability(p,infer(x*EXPOSURE_GAIN),threshold=.5,polygon_skips=skipped)
    if digest(image)!=input_sha:
        raise ValueError('推理期间原图变化，拒绝候选')
    output.mkdir(parents=True,exist_ok=True)
    np.save(output/'candidate-probability.npy',p,allow_pickle=False)
    Image.fromarray((p>=.5).astype('uint8')*255).save(output/'candidate-mask.png')
    rgba=np.zeros((*p.shape,4),dtype='uint8');rgba[p>=.5]=[229,77,255,210] if card['category']=='AR' else [38,214,213,210]
    Image.fromarray(rgba).save(output/'candidate-overlay.png')
    result={'model':card['id'],'image':str(image),'image_sha256':input_sha,'condition':condition,
        'width':size[0],'height':size[1],'created_utc':now(),'release_state':card['release_state'],
        'scientific_test_complete':card['scientific_test_complete'],'score_scope':'区域平均模型分数，未校准',
        'annotations':candidates,'skipped':skipped,'foreground_pixels':int((p>=.5).sum()),'self_check':check,
        'auto_saved_to_original':False,'resources':resources.sample(),
        'assets':{name:digest(output/name) for name in ('candidate-probability.npy','candidate-mask.png','candidate-overlay.png')}}
    atomic_json(output/'candidates.json',result)
    return result


def predict(package,image,condition,output):
    package,image,output=Path(package).resolve(),Path(image).resolve(),Path(output).resolve()
    from .__main__ import DEFAULT_DATA
    output=output_path(output,DEFAULT_DATA)
    if output==image or output.is_relative_to(package):raise ValueError('输出不能覆盖图像或模型包')
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        model,card=load_model(package)
        return predict_loaded(model,card,image,condition,output)


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--condition', choices=['magnetogram', 'AIA193', 'Halpha'], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(predict(args.package, args.image, args.condition, args.output), ensure_ascii=False))
