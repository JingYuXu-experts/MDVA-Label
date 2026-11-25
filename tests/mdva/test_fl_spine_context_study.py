import numpy as np

from mdva.training.fl_spine_candidate_review import geometry_record
from mdva.training.fl_spine_context_study import locate, spine_raster


def test_author_spine_is_limited_to_the_outline_neighborhood():
    record = {'spine': [8, 20, 48, 20], 'segmentation': [[12, 16, 32, 16, 32, 24, 12, 24]]}
    outline, spine, count, drawn, with_geometry = spine_raster([record])

    assert count == 1 and 0 < with_geometry < drawn
    assert outline[20, 20] and spine[20, 20]
    assert not spine[20, 40]


def test_isolated_spine_candidate_can_add_a_strict_miss_without_replacing_existing_mask():
    a = np.zeros((64, 64), dtype=bool)
    a[10:18, 10:18] = True
    local = np.zeros((64, 64), dtype=float)
    local[10:18, 10:18] = .5
    local[36:44, 36:44] = .5
    reference = a.copy()
    reference[36:44, 36:44] = True

    result = locate(local, local, a)
    assert not result[10:18, 10:18].any()
    assert result[36:44, 36:44].all()
    audit = geometry_record(np.ones_like(local), a, local, local, [reference])
    assert audit['expected'] == [True]
    assert audit['oracle_strict_misses'] == 1
