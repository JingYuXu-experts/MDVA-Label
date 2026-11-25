"""Compare saved same-epoch validation logs; no pixel or weight access."""
import argparse,json
from pathlib import Path
from .runtime import digest,now
from .durable import atomic_json
from .run_locations import run_folder


def report(study):
    study=Path(study);root=Path(__file__).resolve().parents[3]
    protocol=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    baseline=run_folder(root/'out/training-20261112-v2','CH-C2-s2026')
    identity=json.loads((baseline/'identity.json').read_text(encoding='utf-8'))
    if identity['data']['manifest-hashes.json']!=protocol['manifest_sha256']:raise ValueError('不是同一冻结数据，不能按轮次比较')
    original=json.loads((baseline/'metrics.json').read_text(encoding='utf-8'))
    path=study/'run/metrics.json';new=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    paired=[]
    for row in new:
        other=next(r for r in original if r['epoch']==row['epoch'])
        paired.append({'epoch':row['epoch'],'original_validation_dice':other['validation']['dice'],
            'new_validation_dice':row['validation']['dice'],'difference_pp':100*(row['validation']['dice']-other['validation']['dice']),
            'new_precision':row['validation']['precision'],'new_recall':row['validation']['recall'],'new_seconds':row['seconds']})
    result={'updated_utc':now(),'state':'development_in_progress','same_frozen_manifest':True,'seed':2026,
        'baseline':'CH-C2-s2026','candidate':'CH-SCSS-GN-s2026','validation_images':526,'same_epoch':paired,
        'historical_candidate_best':max(r['validation']['dice'] for r in original),
        'scope':'Same-epoch single-seed configuration comparison only; not isolated normalization effect, final gain, or independent test.',
        'input_files':{'original_sha256':digest(baseline/'metrics.json'),'candidate_sha256':digest(path) if path.exists() else None}}
    atomic_json(study/'progress-comparison.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',required=True);a=p.parse_args();r=report(a.study)
    print(json.dumps({'same_epoch':r['same_epoch'],'historical_candidate_best':r['historical_candidate_best']}))
