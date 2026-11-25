"""Resolve a recorded forecast revision inside its parent experiment."""
from __future__ import annotations

import json
from pathlib import Path
import re


def active_forecast(experiment):
    experiment = Path(experiment).resolve()
    registry = experiment/'active-forecast.json'
    if not registry.exists():
        return experiment
    record = json.loads(registry.read_text(encoding='utf-8'))
    relative = record.get('relative', '')
    if not re.fullmatch(r'revisions/forecast-v\d+', relative):
        raise ValueError('预测接续记录的版本目录非法')
    base = experiment
    if record.get('parent_experiment'):
        name = record['parent_experiment']
        if not re.fullmatch(r'training-\d{8}-v\d+', name):
            raise ValueError('关联预测实验名称非法')
        base = experiment.parent/name
    folder = (base/relative).resolve()
    if not folder.is_relative_to(base.resolve()) or not base.resolve().is_relative_to(experiment.parent):
        raise ValueError('预测版本越出实验目录')
    return folder
