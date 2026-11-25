"""Compare actual single-image latency after the20-epoch validation review."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .runtime import Resources, digest, exclusive, now, output_path

ROOT = Path(__file__).resolve().parents[3]


def ready(study, review):
    state = json.loads((study/'run/status.json').read_text(encoding='utf-8'))
    if state['state'] not in ('paused', 'completed') or state['completed_epoch'] != 20:
        raise ValueError('训练必须在累计20轮保存并停止，不抢占训练')
    result = json.loads((review/'summary.json').read_text(encoding='utf-8'))
    if (result['state'] != 'completed' or not result['exact_replay']
            or result['test_accessed'] or result['calibration_accessed']
            or result['identity']['checkpoint_kind'] != 'best'):
        raise ValueError('只接受完整验证复算通过的best模型，不允许测试或校准')
    if result['identity']['training_protocol_sha256'] != digest(study/'protocol.json'):
        raise ValueError('训练协议身份改变')
    archive = result['checkpoint_archive']
    if (archive['file'] != 'checkpoint.pt' or not archive['verified']
            or archive['sha256'] != result['identity']['checkpoint_sha256']
            or digest(review/'checkpoint.pt') != archive['sha256']):
        raise ValueError('独立检查点归档身份不符')
    return result


def run(study, review, output):
    import torch
    from .diagnose import benchmark, load_model
    from .reviewed_train import seed_all, load_checkpoint
    from .scss_models import architecture
    from .scss_main import identity
    from .run_locations import run_folder

    study, review = Path(study).resolve(), Path(review).resolve()
    checked = ready(study, review)
    protocol = json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if protocol['code'] != identity():
        raise ValueError('模型训练代码身份改变')
    dataset = Path(protocol['dataset']); output = output_path(output, dataset)
    if output.exists():
        raise ValueError('推理资源报告使用新版本，不覆盖')
    data = SegmentationDataset(dataset, 'CH', 'train')
    row = data.rows[0]; source = data.source(row['uri'])
    record = {'height':512, 'width':512, 'sample_id':sample_id(row['uri'])}
    results = {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        # Recheck after lock acquisition; an intervening run invalidates the review.
        ready(study, review)
        seed_all(2026)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        for candidate in ('original', 'scss'):
            if candidate == 'original':
                model, _, _ = load_model(ROOT/'out/training-20261112-v2', 'CH-C2-s2026', dataset)
                checkpoint_sha = digest(run_folder(ROOT/'out/training-20261112-v2', 'CH-C2-s2026')/'best.pt')
            else:
                saved = load_checkpoint(review/'checkpoint.pt')
                if saved['identity'] != json.loads((study/'run/identity.json').read_text(encoding='utf-8')):
                    raise ValueError('归档检查点训练身份不符')
                model, _ = architecture(protocol['config'])
                model.load_state_dict(saved['model']); del saved
                model.cuda().eval()
                checkpoint_sha = checked['identity']['checkpoint_sha256']
            resource = Resources()
            measured = benchmark(model, record, source, 512, resource)
            measured.update(checkpoint_sha256=checkpoint_sha,
                precision='Both implementations force FP32 forward; outer diagnostic autocast cannot change it; TF32 off')
            results[candidate] = measured
            model.cpu(); del model
            gc.collect(); torch.cuda.empty_cache()
    result = {'state':'completed', 'created_utc':now(), 'review_sha256':digest(review/'summary.json'),
        'source_uri':row['uri'], 'source_sha256':digest(source), 'split':'train',
        'reference_masks_read':False, 'test_accessed':False, 'calibration_accessed':False,
        'results':results, 'scope':'Same cached training image, batch1; each model10 warmups and100 pure plus100 file-to-probability measurements. Not browser or concurrent-load latency.',
        'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_inference_benchmark.py','diagnose.py','scss_models.py','cnn193_fp32.py')},
        'new_to_original_p95_ratio':results['scss']['pure_p95_ms']/results['original']['pure_p95_ms']}
    output.mkdir(parents=True); atomic_json(output/'summary.json', result)
    return result


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--study', required=True)
    parser.add_argument('--review', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.study, args.review, args.output), ensure_ascii=False))
