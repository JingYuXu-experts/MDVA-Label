import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_component_verifier import match_components


def test_component_verifier_matches_one_component_per_original_reference():
    reference = np.zeros((4, 8), dtype=int)
    reference[:, :4] = 17
    raster = np.zeros_like(reference)
    raster[:2, :4] = 1
    raster[2:, :4] = 2
    raster[:, 6:] = 3

    matches = match_components(raster, reference, 3)
    assert sorted(matches[:2]) == [0, 1]
    assert matches[2] == 0


def test_component_verifier_handles_empty_predictions_and_references():
    reference = np.zeros((4, 4), dtype=int)
    assert match_components(np.zeros_like(reference), reference, 0).size == 0
    assert match_components(np.ones_like(reference), reference, 1).tolist() == [0]
