import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_bipolar_proposals import independent_of_mask, proposals, reference_assignment


def test_proposals_require_opposite_contrasts_inside_disk():
    gray = np.full((1024, 1024), 127, dtype=np.uint8)
    gray[500:507, 503:510] = 240
    assert proposals(gray) == []
    gray[502:509, 515:522] = 15
    candidates = proposals(gray)
    assert candidates
    assert all(np.hypot(x - 512, y - 512) < 30 for x, y in (row['center'] for row in candidates))


def test_crop_hit_requires_center_and_half_coverage():
    reference = np.zeros((40, 40), dtype=np.int32)
    reference[10:20, 10:20] = 7
    areas = {7: 100}
    candidate = {'center': [14, 14], 'box': [9, 9, 20, 20]}
    assert reference_assignment(candidate, reference, areas) == (7, 'matched_reference')
    assert reference_assignment({**candidate, 'box': [9, 9, 15, 15]}, reference, areas) == (
        None, 'ambiguous_reference_overlap'
    )
    assert reference_assignment({**candidate, 'center': [9, 9]}, reference, areas) == (
        None, 'ambiguous_reference_overlap'
    )
    assert independent_of_mask(candidate, reference == 0)
    assert not independent_of_mask(candidate, reference > 0)
