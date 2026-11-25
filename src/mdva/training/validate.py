"""Check frozen manifests and real data loading; never trains a classifier/network."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np

from .datasets import (FEATURES, SegmentationDataset, forecast_matrix,
                       freeze_feature_preprocessing, read_frozen)


def validate(directory):
    directory=Path(directory)
    all_names=json.loads((directory/'manifest-hashes.json').read_text(encoding='utf-8'))
    for name in all_names: read_frozen(directory,name)
    segments=read_frozen(directory,'segmentation-ready.jsonl')
    forecasts=read_frozen(directory,'forecast-ready.jsonl')
    for rows, group_keys in [(segments,lambda r:[r['observation_group'],r['pixel_sha256']]+['noaa:'+str(n) for n in r['noaa_ids']]),
                             (forecasts,lambda r:['noaa:'+str(r['noaa'])])]:
        owners=defaultdict(set)
        for row in rows:
            assert row['eligible'] and not row['exclusions']
            for key in group_keys(row): owners[key].add(row['split'])
        assert all(len(v)==1 for v in owners.values()),'冻结集存在跨划分同源记录'
    samples=[]
    for category in ('AR','CH','FL'):
        for split in ('train','validation','calibration','test'):
            ds=SegmentationDataset(directory,category,split,size=512)
            if not len(ds): raise ValueError(f'{category} 的 {split} 是空划分，不能验收')
            for index in sorted({0,len(ds)//2,len(ds)-1}) if len(ds) else []:
                item=ds[index]
                assert tuple(item['image'].shape)==(1,512,512)
                assert item['semantic_mask'].shape==item['image'].shape
                assert tuple(item['instance_mask'].shape)==(512,512)
                assert item['image'].isfinite().all()
                samples.append({'category':category,'split':split,'uri':item['uri']})
    config=freeze_feature_preprocessing(directory)
    diagnostics={}
    feature_dir=directory/'numeric-baseline'
    feature_dir.mkdir(exist_ok=True)
    for split in ('train','validation','calibration','test'):
        x,y,rows=forecast_matrix(directory,split)
        if not rows or len(np.unique(y))<2: raise ValueError(f'{split} 缺少正例或负例，不能用于预测验收')
        assert set(np.unique(y)) <= {0,1}
        filled=np.where(np.isnan(x),np.array(config['medians']),x)
        transformed=(filled-np.array(config['mean']))/np.array(config['scale'])
        assert np.isfinite(transformed).all()
        np.savez_compressed(feature_dir/(split+'.npz'),X=transformed.astype(np.float32),y=y,
                            feature_names=np.array(FEATURES),
                            sample_ids=np.array([r['stem']+':'+str(r['instance_id']) for r in rows]))
        diagnostics[split]={'rows':len(rows),'positives':int(y.sum()),'feature_columns':len(FEATURES),
                            'missing_numeric_cells':int(np.isnan(x).sum())}
    result={'ok':True,'all_manifest_hashes_verified':True,'split_leakage_checks_passed':True,
            'real_image_mask_load_samples':samples,'forecast':diagnostics,
            'feature_fit_split':'train','classifier_or_network_trained':False}
    (directory/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args();print(json.dumps(validate(args.directory),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
