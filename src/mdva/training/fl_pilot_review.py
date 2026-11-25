"""Compare a saved full-data FL checkpoint with the original development baseline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .checkpoint_archive import archive_checkpoint
from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores, sample_id
from .durable import atomic_json
from .fl_author_model import AuthorGraph, SOURCE
from .fl_main import identity
from .metrics import binary_segmentation, segmentation_summary
from .reviewed_train import data_identity, load_checkpoint
from .runtime import Resources, digest, now, output_path

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT/'out/model-diagnostics/20261116-final/case-index.json'


def region_summary(rows):
    fields = ('reference_regions', 'predicted_components', 'matched', 'missed_regions', 'false_positive_regions')
    totals = {key:sum(row[key] for row in rows) for key in fields}
    totals['precision'] = totals['matched']/totals['predicted_components'] if totals['predicted_components'] else 0.
    totals['recall'] = totals['matched']/totals['reference_regions'] if totals['reference_regions'] else None
    return totals


def run(study, output, epoch, checkpoint_kind):
    import torch

    study = Path(study).resolve()
    plan = json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    expected_code = identity()
    if plan.get('sampling') == 'half_inverse':
        from .fl_sampling_control import identity as sampler_identity
        expected_code = sampler_identity()
    if (plan['code'] != expected_code or plan['data_identity'] != data_identity(plan['dataset'])
            or plan['source_provenance_sha256'] != digest(SOURCE/'provenance.json')):
        raise ValueError('试点数据或模型代码身份改变')
    output = output_path(output, plan['dataset'])
    if output.exists() or output.is_relative_to(study):
        raise ValueError('复核需要独立输出目录')
    status = json.loads((study/'run/status.json').read_text(encoding='utf-8'))
    if checkpoint_kind not in ('best', 'last') or not 1 <= epoch <= status['completed_epoch']:
        raise ValueError('只接受完整保存的试点检查点')
    source = study/'run'/f'{checkpoint_kind}.pt'
    sha = digest(source)
    output.mkdir(parents=True)
    archived = archive_checkpoint(source, output/'checkpoint.pt', sha)
    saved = load_checkpoint(archived)
    recorded_identity = json.loads((study/'run/identity.json').read_text(encoding='utf-8'))
    if saved['epoch'] != epoch or saved['identity'] != recorded_identity:
        raise ValueError('保存的轮数或检查点身份不符，暂停复核')
    baseline = [row for row in json.loads(BASELINE.read_text(encoding='utf-8'))
                if row['run_id'] == 'FL-F0-s2026' and row['diagnostic_subset'] == 'validation']
    data = SegmentationDataset(plan['dataset'], 'FL', 'validation', 512)
    if len(data) != 182 or len(baseline) != len(data) or {r['uri'] for r in baseline} != {r['uri'] for r in data.rows}:
        raise ValueError('开发验证与原基线的样本集合不一致')
    torch.set_num_threads(2)
    model = AuthorGraph('batch')
    model.load_state_dict(saved['model'])
    model.eval()
    architecture_parameters = sum(v.numel() for v in model.parameters())
    rows = []
    resource = Resources('cpu')
    with torch.inference_mode():
        for i, row in enumerate(data.rows, 1):
            if (output/'PAUSE').exists():
                atomic_json(output/'status.json', {'state':'paused', 'completed':i-1, 'total':len(data)})
                return {'state':'paused'}
            item = data[i-1]
            logits = model(item['image'][None])
            if not torch.isfinite(logits).all():
                raise ValueError('开发复核输出非有限')
            mask = np.load(data.source(row['mask_uri']), allow_pickle=False) > 0
            prediction = logits.sigmoid()[0, 0].numpy() >= .5
            score = binary_segmentation(prediction, mask, boundary=True)
            regions = connected_scores(prediction, label(mask, structure=np.ones((3,3)))[0])
            row_result = {'uri':row['uri'], 'instrument':row['instrument'], 'score':score, 'regions':regions}
            rows.append(row_result)
            atomic_json(output/'cases'/f'{sample_id(row["uri"])}.json', row_result)
            if i % 25 == 0 or i == len(data):
                atomic_json(output/'status.json', {'state':'validation_inference', 'completed':i, 'total':len(data), 'resource':resource.sample()})
    metrics = segmentation_summary([row['score'] for row in rows])
    expected = saved['validation'] if checkpoint_kind == 'best' else saved['history'][-1]['validation']
    stations = sorted({row['instrument'] for row in rows})
    report = {'created_utc':now(), 'state':'completed', 'epoch':epoch, 'checkpoint_kind':checkpoint_kind,
              'checkpoint_sha256':sha, 'plan_sha256':digest(study/'protocol.json'), 'baseline_sha256':digest(BASELINE),
              'evaluator_sha256':digest(__file__), 'parameters':architecture_parameters, 'metrics':metrics,
              'regions':region_summary([r['regions'] for r in rows]),
              'baseline_metrics':segmentation_summary(baseline), 'baseline_regions':region_summary([r['objects'] for r in baseline]),
              'by_station':{s:{'candidate':segmentation_summary([r['score'] for r in rows if r['instrument']==s]),
                               'baseline':segmentation_summary([r for r in baseline if r['instrument']==s])} for s in stations},
              'gpu_recorded_dice':expected['dice'], 'cpu_minus_gpu_dice':metrics['dice']-expected['dice'],
              'resource':resource.sample(), 'scope':'CPU development validation only, fixed threshold0.5. 8-connected regions at maskIoU0.5. CPU and CUDA may differ.',
              'training':False, 'test_accessed':False, 'calibration_accessed':False, 'models_replaced':False}
    atomic_json(output/'summary.json', report)
    atomic_json(output/'status.json', {'state':'completed', 'completed':len(data), 'total':len(data)})
    return report


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--study', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--epoch', type=int, required=True)
    parser.add_argument('--checkpoint', choices=('best','last'), default='best')
    args = parser.parse_args()
    value = run(args.study, args.output, args.epoch, args.checkpoint)
    print(json.dumps({key:value[key] for key in ('state','metrics','regions') if key in value}))
