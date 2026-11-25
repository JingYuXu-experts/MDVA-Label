import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.forecast_disk_baseline import features, RAW


def test_raw_baseline_is_independent_of_detected_regions():
    image = np.full((1024, 1024), 127, dtype=np.uint8)
    image[510:515, 510:515] = 200
    blank = np.zeros_like(image, dtype=bool)
    populated = blank.copy()
    populated[100:104, 200:203] = True
    populated[300:303, 250:251] = True
    empty, detected = features(image, blank), features(image, populated)
    assert all(empty[key] == detected[key] for key in RAW)
    assert detected['region_count'] == 2
    assert detected['largest_share'] == pytest.approx(.8)
    assert empty['region_fraction'] == empty['largest_share'] == 0.
