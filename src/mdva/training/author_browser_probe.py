"""Fixed original-browser CNN193 weights, validation only; no training or tuning."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .author_tfjs import load, native_binary
from .ch_display_probe import localization_record
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .localization_audit import aggregate
from .metrics import binary_segmentation, segmentation_summary
from .regional_decision import paired_intervals
from .runtime import Resources, digest, exclusive, now, output_path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT/'out/solar-model-sources/cnn193-browser-20261116'


def run(dataset,output,resume=False):
    import torch
    dataset = Path(dataset).resolve(); output = output_path(output,dataset)
    preparation = json.loads((output/'input-preparation.json').read_text(encoding='utf-8'))
    cache = json.loads((output/'input-cache.json').read_text(encoding='utf-8'))
    parity_path = SOURCE/'numerical-parity.json'
    parity = json.loads(parity_path.read_text(encoding='utf-8'))
    code = {n:digest(Path(__file__).with_name(n)) for n in ('author_tfjs.py','author_model_snapshot.py','author_browser_probe.py')}
    if not parity['passed'] or parity['importer_sha256'] != code['author_tfjs.py']:
        raise ValueError('作者运行时数值核验未通过或转换代码改变')
    if preparation['scope'] != 'CH validation only' or preparation['manifest_sha256'] != digest(dataset/'manifest-hashes.json'):
        raise ValueError('只允许冻结CH完整验证集')
    if cache['state'] != 'completed' or cache['preparation_sha256'] != digest(output/'input-preparation.json') or cache['external_requests'] != 0:
        raise ValueError('本地浏览器输入缓存不完整或来源改变')
    if digest(SOURCE/'native-input-cache.cjs') != preparation['cache_script_sha256'] or digest(SOURCE/'index.js') != preparation['native_bundle_sha256']:
        raise ValueError('输入生成代码改变')
    data = SegmentationDataset(dataset,'CH','validation')
    cached = {r['uri']:r for r in cache['rows']}
    if len(cached) != 526 or {r['uri'] for r in data.rows} != set(cached): raise ValueError('图像集合不同')
    historical = ROOT/'out/model-diagnostics/20261116-final/case-index.json'
    control = [r for r in json.loads(historical.read_text(encoding='utf-8'))
               if r['run_id']=='CH-C2-s2026' and r['diagnostic_subset']=='validation']
    if len(control)!=526 or {r['uri'] for r in control}!=set(cached): raise ValueError('基线集合不同')
    control_locations_path=ROOT/'out/recognition-improvement-20261116/display-probe-01/cases.json'
    control_locations=json.loads(control_locations_path.read_text(encoding='utf-8'))['original']
    if len(control_locations)!=526 or {r['uri'] for r in control_locations}!=set(cached): raise ValueError('基线区域集合不同')
    identity = {'created_scope':'author browser weights, validation only; no training/test/calibration',
        'preparation_sha256':digest(output/'input-preparation.json'),'cache_sha256':digest(output/'input-cache.json'),
        'parity_sha256':digest(parity_path),'snapshot_sha256':digest(SOURCE/'keras/snapshot.json'),
        'historical_control_sha256':digest(historical),'code':code,'native_binary_rule':'floor(linear+.5)==1',
        'control_locations_sha256':digest(control_locations_path),
        'resolution':256,'binary_resize':'PIL nearest to original512','original_models_replaced':False}
    if (output/'identity.json').exists():
        if not resume or json.loads((output/'identity.json').read_text(encoding='utf-8')) != identity:
            raise ValueError('已有评价身份不同或未请求恢复，拒绝覆盖')
    else:
        atomic_json(output/'identity.json',identity)
        for name in code:
            p=output/'source'/name;p.parent.mkdir(exist_ok=True);p.write_bytes(Path(__file__).with_name(name).read_bytes())
    scores=[];locations=[];linear=[]
    from .journal import append
    experiment = ROOT/'out/training-20261112-v2'
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.set_num_threads(4);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        model,_ = load(SOURCE/'keras','cuda');resource=Resources()
        with torch.inference_mode():
            check_input=np.fromfile(SOURCE/'parity-02/image256.input.f32',dtype='<f4').reshape(1,1,256,256)
            expected_native=np.fromfile(SOURCE/'parity-02/image256-conv2d_95.native.f32',dtype='<f4').reshape(256,256)
            check_output=model(torch.from_numpy(check_input.copy()).cuda())[0,0].cpu().numpy()
            if not np.allclose(check_output,expected_native,atol=2e-5,rtol=2e-5) or not np.array_equal(native_binary(check_output),native_binary(expected_native)):
                raise ValueError('GPU与作者原运行时输出不一致，停止')
            atomic_json(output/'gpu-parity.json',{'passed':True,'max_absolute_difference':float(abs(check_output-expected_native).max()),
                'native_binary_mismatches':0,'resource':resource.sample()})
            for i,row in enumerate(data.rows):
                if (output/'PAUSE').exists():
                    atomic_json(output/'status.json',{'state':'paused','completed':i,'total':len(data),'training':False})
                    return {'state':'paused'}
                sid=sample_id(row['uri']);folder=output/'cases'/sid;saved=folder/'result.json';item=cached[row['uri']]
                image=data.source(row['uri'])
                if item['source_sha256']!=digest(image): raise ValueError('源图像改变')
                input_path=(output/item['file']).resolve()
                if not input_path.is_relative_to((output/'inputs').resolve()) or digest(input_path)!=item['input_sha256']:
                    raise ValueError('输入缓存身份改变或路径越界')
                if saved.exists():
                    r=json.loads(saved.read_text(encoding='utf-8'))
                    if r['identity_sha256']!=digest(output/'identity.json') or digest(folder/'native.png')!=r['mask_sha256']:
                        raise ValueError('逐图结果身份改变')
                    data.source(row['mask_uri'])
                else:
                    x=np.fromfile(input_path,dtype='<f4')
                    if x.size!=256**2 or not np.isfinite(x).all() or x.min()<0 or x.max()>1: raise ValueError('输入缓存数值非法')
                    raw=model(torch.from_numpy(x.reshape(1,1,256,256).copy()).cuda())[0,0].cpu().numpy()
                    native=native_binary(raw)
                    prediction=np.asarray(Image.fromarray(native.astype('uint8')).resize((512,512),Image.Resampling.NEAREST))>0
                    # Only after inference is the reference loaded.
                    mask=np.load(data.source(row['mask_uri']),allow_pickle=False)
                    if mask.shape!=(512,512): raise ValueError('参考原始尺寸不符')
                    score=binary_segmentation(prediction,mask>0,boundary=True)
                    folder.mkdir(parents=True,exist_ok=True)
                    Image.fromarray(prediction.astype('uint8')*255).save(folder/'native.png')
                    r={'uri':row['uri'],'identity_sha256':digest(output/'identity.json'),
                       'score':score,'localization':localization_record(row,prediction,mask,score,'author_browser_native'),
                       'linear_output':{'min':float(raw.min()),'max':float(raw.max()),
                                        'pixels_ge_1_5':int((raw>=1.5).sum()),'native_positive_pixels':int(native.sum())},
                       'mask_sha256':digest(folder/'native.png')}
                    atomic_json(saved,r)
                scores.append(r['score']);locations.append(r['localization']);linear.append(r['linear_output'])
                if (i+1)%50==0 or i+1==len(data):
                    status={'state':'inference_only','completed':i+1,'total':len(data),'training':False,'updated_utc':now(),'resource':resource.sample()}
                    atomic_json(output/'status.json',status);print(json.dumps(status,ensure_ascii=False),flush=True)
                    append(experiment,'development','作者CNN193权重验证推理',f'{i+1}/{len(data)}，仅验证集，原演示输出规则，未训练。')
    metrics=segmentation_summary(scores);control_metrics=segmentation_summary(control);localization=aggregate(locations)
    control_localization=aggregate(control_locations)
    limits=preparation['candidate_gate']
    checks={'dice_gain':metrics['dice']>=control_metrics['dice']+limits['dice_gain'],
            'half_coverage_gain':localization['reference_coverage']['half_covered_fraction']>=control_localization['reference_coverage']['half_covered_fraction']+limits['half_coverage_gain'],
            'precision_preserved':metrics['precision']>=control_metrics['precision']-limits['max_precision_drop']}
    report={'state':'completed','updated_utc':now(),'identity':identity,'metrics':metrics,'control_metrics':control_metrics,
        'localization':localization,'control_localization':control_localization,
        'paired_uncertainty':paired_intervals(control_locations,locations),
        'empty_predictions':sum(r['predicted_pixels']==0 for r in scores),
        'linear_output_range':[min(r['min'] for r in linear),max(r['max'] for r in linear)],
        'native_rounding_excluded_ge1_5_pixels':sum(r['pixels_ge_1_5'] for r in linear),'checks':checks,
        'decision':'further_input_and_licensing_review_required' if all(checks.values()) else 'do_not_deploy_author_browser_weights_on_this_input_domain',
        'training':False,'test_accessed':False,'calibration_accessed':False,'models_replaced':False,
        'resource':resource.sample(),'scope_warning':'Exploratory repeated validation only; native browser model outputs are not calibrated probabilities.',
        'empty_reference_rate':'No independent empty-reference cohort; not measured'}
    atomic_json(output/'summary.json',report);atomic_json(output/'locations.json',locations)
    atomic_json(output/'status.json',{'state':'completed','completed':len(data),'total':len(data),'training':False,'updated_utc':now()})
    append(experiment,'handoff','作者CNN193权重验证结束',report['decision'],details={'report':str(output/'summary.json')})
    return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--output',required=True);p.add_argument('--resume',action='store_true')
    a=p.parse_args();r=run(a.dataset,a.output,a.resume);print(json.dumps({'state':r['state'],'decision':r.get('decision')},ensure_ascii=False))
