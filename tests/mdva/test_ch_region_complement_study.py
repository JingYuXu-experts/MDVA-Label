import numpy as np

from mdva.training.ch_region_complement_study import (
    non_conflicting,
    option_geometry,
    score_geometry,
)


def test_addition_recovers_a_missed_region_and_rejects_multi_region_replacement():
    primary = np.zeros((512, 512), dtype=np.int32)
    primary[30:40, 20:28] = 1
    primary[30:40, 35:43] = 2
    reference = primary.copy()
    reference[60:70, 60:71] = 3
    secondary = np.zeros_like(primary)
    secondary[30:40, 20:43] = 1
    secondary[60:70, 60:71] = 2
    scores = np.full(primary.shape, 0.7, dtype=np.float32)
    gray = np.full(primary.shape, 90, dtype=np.uint8)

    original, geometry, proposals = option_geometry(
        primary, secondary, scores, scores, gray, reference,
    )

    assert original['matched'] == 2
    assert [p['kind'] for p in proposals] == ['addition']
    assert proposals[0]['improves']
    result = score_geometry(geometry, proposals, set(), [0])
    assert result['matched'] == result['predicted'] == 3


def test_higher_priority_replacement_reserves_its_primary_region():
    proposals = [{'replaces': 3}, {'replaces': None}, {'replaces': 3}]

    assert non_conflicting(proposals, [2, 1, 0]) == ({3}, [2, 1])
