import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_hmi_checkpoint_pipeline import describe_additions


def test_addition_diagnostic_distinguishes_reference_match_and_disjoint_region():
    raster = np.zeros((12, 12), dtype=np.int32)
    raster[1:3, 1:3] = 1
    raster[7:9, 7:9] = 2
    reference = np.zeros_like(raster)
    reference[1:3, 1:3] = 15
    parts = [{'area': 4, 'state': 'physical_unavailable'} for _ in (0, 1)]
    rows = describe_additions(raster, raster > 0, reference, parts, {}, {15})
    assert rows[0]['classification'] == 'recovered_reference'
    assert rows[0]['best_iou'] == 1.
    assert rows[1]['classification'] == 'disjoint_from_reference'
    assert rows[1]['reference_distance_pixels'] == pytest.approx(np.sqrt(50))
    assert not describe_additions(raster, np.zeros_like(raster, dtype=bool), reference, parts, {}, set())
