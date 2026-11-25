"""Review unannotated CH candidates using registered EUV and HMI observations."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt, find_objects, label
import torch

from .ch_boundary_growth import CONNECTIVITY
from .ch_dense_multichannel import cards, physical
from .ch_matched_tiles import CORE, RADIUS, channel_files
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .inference import load_model
from .journal import read
from .recognition_approximate_audit import pair_regions
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .ar_scale_study import ROOT


CONTRAST = {'193': .80, '211': .85}
FIELD_FLOOR = 5.
DOMINANCE = .75
MIN_AREA = 64
CORE_SUPPORT = .95


def diagnostics(component, arrays, valid, occupied):
    included = component & valid
    if (component.sum()<MIN_AREA or included.sum()<CORE_SUPPORT*component.sum()):
        return {'spectral_state':'outside_verified_support','field_state':'insufficient_support'}
    distance = distance_transform_edt(~component)
    neighborhood = valid & ~occupied & (distance>=3) & (distance<=16)
    if neighborhood.sum()<MIN_AREA:
        return {'spectral_state':'insufficient_background','field_state':'insufficient_support'}
    ratios = {}
    for name in ('193','211','171'):
        background = float(np.median(arrays[name][neighborhood]))
        if background<=0.:
            return {'spectral_state':'unusable_background','field_state':'insufficient_support'}
        ratios[name] = float(np.median(arrays[name][included]))/background
    dark = all(ratios[k]<=q for k,q in CONTRAST.items())
    result = {'spectral_state':'coincident_darkness' if dark else 'channels_do_not_support',
              'candidate_to_local_background':ratios}
    if 'hmi' not in arrays:
        return {**result,'field_state':'unavailable'}
    pixels = arrays['hmi'][included]
    finite = np.isfinite(pixels)
    significant = pixels[finite & (np.abs(pixels)>=FIELD_FLOOR)]
    if finite.mean()<.95 or len(significant)<.20*len(pixels):
        return {**result,'field_state':'insufficient_magnetic_signal',
                'valid_field_share':float(finite.mean()),'field_significant_share':len(significant)/len(pixels)}
    pos = float((significant>0).mean())
    magnitude = float(abs(significant.sum())/np.abs(significant).sum())
    qualified = max(pos,1-pos)>=DOMINANCE and magnitude>=.50
    return {**result,'field_state':'dominant_polarity' if qualified else 'mixed_polarity',
            'field_dominant_sign':'positive' if pos>=.5 else 'negative',
            'field_dominant_share':max(pos,1-pos),'field_flux_imbalance_proxy':magnitude,
            'field_significant_share':len(significant)/len(pixels)}


def classify(support, overlap):
    """Hold physical evidence separately from the original reference-match label."""
    if overlap:
        return 'reference_overlap'
    if support['spectral_state']=='coincident_darkness' and support['field_state']=='dominant_polarity':
        return 'high_suspicion'
    return 'needs_review'


def audited(audit, source):
    file = audit/source/'summary.json'
    record = read(file)
    if record['state']!='completed' or digest(file.with_name('protocol.json'))!=record['protocol_sha256']:
        raise ValueError('原多源资料的身份无效')
    return {r['uri']:r for r in record['results']},digest(file)


def fetch_arrays(folder, row, original, extra, magnet, header):
    if magnet is None:
        return physical(folder,row,original,extra)
    geometry, measured = magnet[row['uri']],header[row['uri']]
    if not (geometry['state']=='angular_sampled_not_physically_certified' and geometry['BUNIT']=='Gauss'
            and geometry['QUALITY']==0 and measured['quality_zero'] and measured['temporal_gate_passed']
            and geometry['native_hmi_sha256']==measured['sha256']
            and geometry['numerical_roundtrip_max_pixels']<1e-4):
        return physical(folder,row,original,extra)
    pair = folder/'aia'/'pairs'/row['id']
    # The channel loader checks native AIA and resampled HMI identities together.
    if digest(pair/'mapping-review.json')!=original['mapping_sha256']:
        raise ValueError('EUV与显示图几何证据变化')
    return channel_files(folder,(extra,geometry),pair)


def run(dataset, cohorts, package, output):
    output = output_path(output,dataset)
    if output.exists():
        raise ValueError('多源复核必须使用新的成果目录')
    training = SegmentationDataset(dataset,'CH','train',512)
    index = {r['uri']:r for r in training.rows}
    sufficient = {}
    attached = []
    for folder in cohorts:
        conf = folder/'selection.json'
        if read(conf)['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):
            raise ValueError('复核输入不属于冻结训练清单')
        extra,magnet = None,None
        receipts = {}
        if (folder/'angular'/'summary.json').exists():
            magnet, receipts['hmi_angular_sha256'] = audited(folder,'angular')
            extra, receipts['hmi_source_sha256'] = audited(folder,'hmi')
        candidates = list(cards(folder,conf))
        sufficient[folder.name] = {'selection_sha256':digest(conf),'dates':len(candidates),**receipts,
                                    'aia_sha256':digest(folder/'aia'/'summary.json'),
                                    'aia171_sha256':digest(folder/'aia171'/'summary.json')}
        attached.extend((folder,row,orig,new,magnet,extra) for row,orig,new in candidates)
    uris = [row['uri'] for _,row,*_ in attached]
    if len(set(uris))!=len(uris) or set(uris)-set(index):
        raise ValueError('重叠或保护划分资料不能进入复核')
    policy = {'code_sha256':digest(__file__),'sources':sufficient,'threshold':.5,'euv_ratio_max':CONTRAST,
              'dominance_floor':DOMINANCE,'significant_los_gauss':FIELD_FLOOR,
              'model_package_sha256':digest(package/'model.json'),
              'scope':'Offline candidate triage with local display geometry; both separate EUV and HMI evidence required for high suspicion',
              'validation_accessed':False,'calibration_accessed':False,'test_accessed':False}
    atomic_json(output/'protocol.json',policy)
    results = []
    totals = Counter()
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        model,meta = load_model(package)
        if meta['id']!='CH-C2-s2026' or meta['config']['size']!=512:
            raise ValueError('只复核已登记的 CH-C2 模型')
        meter = Resources()
        for folder,row,original,additional,magnet,hmi in attached:
            image = training.source(row['uri'])
            if digest(image)!=row['sha256']:
                raise ValueError('显示图与佐证身份不同')
            arrays = fetch_arrays(folder,row,original,additional,magnet,hmi)
            with Image.open(image) as file:
                gray = np.asarray(file.convert('L'),dtype=np.float32)/255.
            probability = model(torch.from_numpy(gray[None,None]).cuda()).float().sigmoid()[0,0].cpu().numpy()
            if probability.shape!=(512,512) or not np.isfinite(probability).all():
                raise ValueError('候选概率网格无效')
            binary = probability>=.5
            objects,count = label(binary,structure=CONNECTIVITY)
            references = np.load(training.source(index[row['uri']]['mask_uri']),allow_pickle=False)
            approximate = pair_regions(binary,references,'CH')
            physical_support = (RADIUS<=CORE) & np.isfinite([arrays[k] for k in ('171','193','211')]).all(axis=0)
            physical_support &= arrays['193']>0.
            records = []
            for i,box in enumerate(find_objects(objects),1):
                component = objects==i
                checks = diagnostics(component,arrays,physical_support,binary)
                overlap = int((component & (references>0)).sum())
                state = classify(checks,overlap)
                totals[state] += 1
                totals[checks['spectral_state']] += 1
                records.append({'candidate':i,'state':state,'reference_overlap_pixels':overlap,
                    'bbox_xyxy':[box[1].start,box[0].start,box[1].stop,box[0].stop],
                    'pixels':int(component.sum()),**checks})
            mask = output/'candidate-masks'/f"{row['id']}.npz"
            commit(mask,lambda f:np.savez_compressed(f,components=objects,reference=references>0))
            results.append({'uri':row['uri'],'time_utc':index[row['uri']]['time_utc'],'source':folder.name,
                'known_reference_metrics':approximate,'channels':list(arrays),'candidate_count':count,'candidates':records,
                'aia_display_sha256':row['sha256'],'aia_mapping_sha256':original['mapping_sha256'],
                'aia171_sha256':additional['sha256'],
                'hmi_array_sha256':magnet[row['uri']]['array_sha256'] if 'hmi' in arrays else None,
                'mask_artifact':mask.relative_to(output).as_posix(),'mask_sha256':digest(mask)})
            meter.sample()
        resources=meter.sample()
    atomic_json(output/'cases.json',results)
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),'images':len(results),
        'images_with_valid_magnetic_context':sum('hmi' in r['channels'] for r in results),
        'counts':dict(totals),'protocol_sha256':digest(output/'protocol.json'),
        'cases_sha256':digest(output/'cases.json'),'resources':resources,
        'validation_accessed':False,'calibration_accessed':False,'test_accessed':False,'deployed':False})


if __name__=='__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','package','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--cohorts',required=True,type=Path,nargs='+')
    run(**vars(parser.parse_args()))
