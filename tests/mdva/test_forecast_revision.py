from copy import deepcopy

import pytest

from mdva.training.forecast_revision import canonicalize
from mdva.training.context import active_forecast


def row(instance, line):
    return {'split': 'train', 'time_utc': '2014-03-01T00:00:00+00:00', 'noaa': 11999,
            'features': {name: 1. for name in ('MGB_PIL','R','MGB','Total_Unsigned_Flux','Area')},
            'target_m_plus': 0, 'target_start': '2014-03-01T00:00:00+00:00',
            'target_end': '2014-03-02T00:00:00+00:00', 'forecast_horizon_hours': 24,
            'instance_id': instance, 'parameter_line': line,
            'parameters_uri': '@source/parameters.txt', 'stem': 'same-observation'}


def test_numeric_ar_day_duplicates_collapse_only_with_equal_inputs_and_target():
    rows = [row(2,2), row(2,3), row(4,4)]
    original = deepcopy(rows)
    ready, duplicates, conflicts = canonicalize(rows)
    assert len(ready) == len(duplicates) == 1 and not conflicts
    assert ready[0]['sample_unit'] == 'NOAA_AR_day'
    assert ready[0]['source_record_count'] == 3
    assert [r['instance_id'] for r in ready[0]['provenance_associations']] == [2,2,4]
    assert rows == original
    rows[1]['features']['Area'] = 2.
    ready, _, conflicts = canonicalize(rows)
    assert not ready and len(conflicts) == 1
    assert '不一致' in conflicts[0]['reason']


def test_cross_experiment_forecast_link_is_confined_to_named_sibling(tmp_path):
    import json
    parent = tmp_path/'training-20261112-v2'; parent.mkdir()
    (parent/'active-forecast.json').write_text(json.dumps({
        'parent_experiment': 'training-20261112-v1', 'relative': 'revisions/forecast-v2'}))
    assert active_forecast(parent) == tmp_path/'training-20261112-v1/revisions/forecast-v2'
    (parent/'active-forecast.json').write_text(json.dumps({
        'parent_experiment': '../../secret', 'relative': 'revisions/forecast-v2'}))
    with pytest.raises(ValueError): active_forecast(parent)
