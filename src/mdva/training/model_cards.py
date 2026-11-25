"""Write explicit research model cards without changing frozen results or weights."""
from __future__ import annotations

import json
from pathlib import Path

from .forecast_delivery import registered_results
from .runtime import atomic_json, digest


def forecast_cards(experiment):
    experiment = Path(experiment)
    total = 0
    for relative in registered_results():
        file = experiment/'forecast'/relative
        if not file.exists():
            continue
        result = json.loads(file.read_text(encoding='utf-8'))
        neural = bool(result.get('kind'))
        steps = result.get('steps', 3 if result.get('input') == 'three_daily_points_flattened' else 1)
        weights = file.parent/('weights.safetensors' if neural else file.name.replace('-result.json', '.joblib'))
        identifier = relative.replace('/', '--').replace('.json', '')
        testfile = experiment/'forecast-test'/identifier/'summary.json'
        card = {'id': identifier, 'task': 'numbered_AR_M_plus_next24h_reference_research',
                'release_state': 'research_only_not_operational', 'original_mask_is_unchanged': True,
                'target': '本数据参考事件目录定义的M1.0+，(T,T+24h]，已编号AR',
                'features': result.get('features', result.get('preprocessing', {}).get('features')) or ['MGB_PIL','R','MGB','Total_Unsigned_Flux','Area'],
                'input_condition': '来源一致的活动区数值参数；当前任务不能只靠显示域JPG还原这些物理参数',
                'units': '沿用原始参数表；R等字段的独立物理单位/对数约定尚未核验，不擅自标注单位',
                'history': {'input_observation_points': steps,
                            'cohort_required_points': result.get('common_steps') or (3 if result.get('cohort') == 'sequence3' else steps),
                            'daily_seconds_tolerance': 2, 'same_region_required': True,
                            'repeat_current_control': result.get('repeat_current', False)},
                'weights': weights.name, 'weights_sha256': digest(weights),
                'serialization': 'safetensors' if neural else 'joblib (trusted self-produced local files only; never accept uploaded pickle)',
                'result_sha256': digest(file), 'dataset_manifest_sha256': result.get('data', result.get('dataset')),
                'calibration': '2022 independent split; never fit on test',
                'threshold': result['threshold'], 'training_resource': result.get('resource'),
                'validation': result['validation_calibrated'],
                'reference_test': json.loads(testfile.read_text(encoding='utf-8'))['calibrated'] if testfile.exists() else None,
                'limitations': ['参考目录与NOAA科学级目录有等级及覆盖差异，不能称权威完整标签',
                               '原始输入历史发布时间未证明，不得称已通过业务实时预报验收',
                               '活动区风险不是全日面风险，也不等同CME到达或通信链路故障概率',
                               '2021开发分数应用了2022校准，属于回顾开发，不能冒充2021实时表现'],
                'implementation_license': 'MDVA adaptation GPL-3.0; see repository license'}
        atomic_json(file.parent/(file.stem+'-model-card.json'), card)
        text = f"# {identifier}\n\n状态：仅限参考目录回顾研究，未获业务实时发布许可。\n\n"
        text += '目标：'+card['target']+'。\n\n输入：'+card['input_condition']+'。\n\n'
        text += '测试、资源、预处理和权重哈希见同名 model-card.json；原始结果不覆盖。\n\n'
        text += '\n'.join('- '+value for value in card['limitations'])+'\n'
        (file.parent/(file.stem+'-MODEL_CARD.md')).write_text(text, encoding='utf-8')
        total += 1
    return total
