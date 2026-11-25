"""Evidence-based interpretation of cached diagnostics, never changes labels."""
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from .diagnostic_support import read, write
from .runtime import digest, now


def shifted(a, dy, dx):
    result = np.zeros_like(a)
    h, w = a.shape
    ys, ye = max(0, -dy), min(h, h-dy)
    xs, xe = max(0, -dx), min(w, w-dx)
    if ys < ye and xs < xe:
        result[ys+dy:ye+dy, xs+dx:xe+dx] = a[ys:ye, xs:xe]
    return result


def review(output):
    output = Path(output)
    report, audit, cases = (read(output/name) for name in ('report.json', 'audit.json', 'case-index.json'))
    models = [m for m in report['models'] if m['state'] == 'completed']
    groups = report['groups']
    station_table, fit_table, thresholds, performance = [], [], [], []
    for group in groups:
        ms = [m for m in models if m['id'].rsplit('-s', 1)[0] == group['group'] and not m['partial_training']]
        for station, values in ms[0]['strata']['instrument'].items():
            station_table.append({'group': group['group'], 'station': station, 'validation_images': values['images'],
                'validation_dice': float(np.mean([m['strata']['instrument'][station]['dice'] for m in ms])),
                'validation_recall': float(np.mean([m['strata']['instrument'][station]['recall'] for m in ms]))})
            train = [m['training_diagnostics']['instrument'][station] for m in ms]
            fit_table.append({'group': group['group'], 'station': station, 'training_nonempty_images': train[0]['nonempty_images'],
                'sampled_training_dice': float(np.mean([v['dice'] for v in train if v['dice'] is not None])),
                'validation_dice': station_table[-1]['validation_dice'],
                'limitation': '训练抽样与验证日期不同，仅提供拟合/分布差异线索，不是独立过拟合证明'})
        ts = ms[0]['threshold_sensitivity']
        curve = [{'threshold': t['threshold'], 'dice': float(np.mean([m['threshold_sensitivity'][i]['dice'] for m in ms]))}
                 for i, t in enumerate(ts)]
        best = max(curve, key=lambda a: a['dice'])
        thresholds.append({'group': group['group'], 'fixed_05_dice': group['mean_dice'],
                           'exploratory_peak': best, 'gain_percentage_points': (best['dice']-group['mean_dice'])*100,
                           'deployment_threshold_changed': False})
        performance.append({'group': group['group'], 'parameters': ms[0]['performance']['parameters'],
                            'pure_p95_ms': group['pure_p95_ms'], 'peak_allocated_gib': group['peak_allocated_gib']})
    roundtrip = [r['mask_512_roundtrip']['dice'] for r in audit['items']
                 if r['category'] == 'AR' and r['split'] == 'validation' and r['mask_512_roundtrip']['dice'] is not None]
    suspects = []
    # Small translations are sensitivity probes, not an alignment correction.
    for run_id in ('FL-F0-s17', 'FL-F2-s17', 'AR-A1-s17', 'CH-C2-s17'):
        candidates = sorted([r for r in cases if r['run_id'] == run_id and r['split'] == 'validation'], key=lambda a: a['dice'])[:5]
        for row in candidates:
            ref = np.asarray(Image.open(output/'assets'/row['assets']['reference']))[:, :, 3] > 0
            pred = np.asarray(Image.open(output/'assets'/row['assets']['prediction']))[:, :, 3] > 0
            best, offset = row['dice'], [0, 0]
            for dy in (-8, -4, 0, 4, 8):
                for dx in (-8, -4, 0, 4, 8):
                    y = shifted(ref, dy, dx)
                    denom = int(pred.sum())+int(y.sum())
                    value = 2*int((pred & y).sum())/denom if denom else 0
                    if value > best:
                        best, offset = value, [dy, dx]
            kinds = ['严重漏分/边界偏差待视觉复核']
            if best-row['dice'] >= .05:
                kinds.append('小位移敏感，疑似局部错位或边界偏差，未确认配准错误')
            if row['precision'] < .5:
                kinds.append('参考外预测较多：可能误检或标注范围差异，需专家判断')
            suspects.append({'run_id': run_id, 'category': row['category'], 'sample_id': row['sample_id'],
                'uri': row['uri'], 'dice': row['dice'], 'kinds': kinds,
                'translation_probe': {'range_pixels': [-8, 8], 'best_delta_y_x': offset,
                                      'dice_gain': best-row['dice'], 'reference_modified': False}})
    findings = [
        {'title': '未发现历史指标复算偏差', 'kind': 'verified',
         'detail': f"{len(models)} 个检查点已完成复算；最大Dice绝对差 {max(m['reproduction_delta'] for m in models):.8f}。未据此证明标注语义或真实方向正确。"},
        {'title': '台站差异需要优先定位', 'kind': 'evidence',
         'detail': '查看下方各台站结果；训练约91%来自BBSO，验证包含较多HSOS。台站、年份、图像质量与标注风格相互混杂，不能仅凭差值确定单一原因。'},
        {'title': '缩小图像不是暗条当前流程中的直接步骤', 'kind': 'verified',
         'detail': '训练/验证暗条原图与主配置输入均为512×512。当前不会再从更高原图降至512；不能据此排除数据制作阶段已经损失细节。'},
        {'title': '调阈值不等于解决质量问题', 'kind': 'evidence',
         'detail': '已固定扫描0.10～0.90，观察上界见阈值表。它使用重复开发的验证集，不自动更改阈值，也不是新的测试成绩。'},
        {'title': '参数少并不保证推理更快', 'kind': 'verified',
         'detail': '本机统一batch 1实测表同时列参数量、推理P95与显存。Flat注意力包含数值稳定性所需的FP32运算；当前未证明其部署收益优于标准U-Net。'},
        {'title': '白色日面外背景不是已确认坏图', 'kind': 'limitation',
         'detail': f"亮边框启发式对全部AR/CH共{sum(r['category'] in ('AR','CH') for r in audit['items'])}对样本触发，缺乏异常区分度；这些是格式复查提示，不能当作错误或删除依据。"},
        {'title': '缺少两项产品证据', 'kind': 'limitation',
         'detail': '验证集没有空参考图，且未开展人工计时；不能确认独立无目标图误报率，也不能声称自动候选能省时。'},
    ]
    value = {'updated_utc': now(), 'report_sha256': digest(output/'report.json'),
             'review_code_sha256': digest(__file__), 'findings': findings, 'station_table': station_table,
             'fit_table': fit_table, 'threshold_observations': thresholds, 'performance': performance,
             'review_cases': suspects, 'AR_validation_mask_roundtrip_512': {
                 'images': len(roundtrip), 'mean_dice': float(np.mean(roundtrip)), 'minimum_dice': float(np.min(roundtrip)),
                 'note': '仅掩膜缩放往返保真，不是模型分数，也不是1024输入模型结果'},
             'pixels_changed': False, 'test_accessed': False, 'automatic_training_restart': False,
             'next_experiments': [
                 {'decision': '保留', 'target': 'FL-F0', 'reason': '作为当前数据下的标准基线，不再重复训练已完成种子。'},
                 {'decision': '暂缓', 'target': '新增FL结构/宽度实验', 'reason': '先复核台站与严重漏分案例；现有27项保持完整留档。'},
                 {'decision': '条件继续', 'target': '原协议AR/CH剩余实验', 'reason': '用户确认诊断后，先修复WinError 5并验证恢复，再补齐基线和重复种子。'},
                 {'decision': '不安排', 'target': '自动发布与预测扩展', 'reason': '本轮没有人工效率、独立测试和预测监督放行依据。'}]}
    write(output/'interpretation.json', value)
    from .run_locations import run_folder
    experiment = Path(read(output/'identity.json')['experiment'])
    actions = []
    for config in read(experiment/'protocol.json')['recognition']:
        state = read(run_folder(experiment, config['id'])/'status.json', {'state': 'not_started'})
        actions.append({'id': config['id'], 'current_state': state['state'],
            'proposed_action': 'retain_no_retrain' if state['state'] == 'completed' else 'audit_existing_checkpoint_before_resume' if state['state'] == 'failed' else 'conditional_original_protocol',
            'requires_user_confirmation': state['state'] != 'completed'})
    write(output/'next-experiment-proposal.json', {'state': 'proposal_not_executed', 'training_restart': False,
        'original_protocol_changed': False, 'test_unlock': False, 'actions': actions,
        'preconditions': ['用户确认诊断取舍', '修复并验证WinError 5', '源码变更先审计实验身份，禁止绕过冻结检查'],
        'AR_failed_checkpoint_note': '第26轮已保存且达到原patience边界；恢复前核对早停语义，不直接补跑或手工标为完成'})
    lines = ['# 诊断结论与下一步', '', *[f"## {r['title']}\n\n{r['detail']}\n" for r in findings],
             '## 台站分层（验证集）', '', '| 配置 | 台站 | 图像数 | Dice |', '|---|---|---:|---:|',
             *[f"| {r['group']} | {r['station']} | {r['validation_images']} | {r['validation_dice']*100:.2f}% |" for r in station_table],
             '', '## 后续实验取舍', *[f"- {r['decision']}：{r['target']}。{r['reason']}" for r in value['next_experiments']],
             '', '20个待复查案例、位移敏感性、参数量和阈值观察详见 interpretation.json。原掩膜、训练配置和最终测试均未改动。']
    (output/'interpretation.md').write_text('\n'.join(lines), encoding='utf-8')
    return value


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--report', type=Path, required=True)
    review(p.parse_args().report)
