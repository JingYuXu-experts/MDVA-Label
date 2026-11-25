import numpy as np

from mdva.training.fl_native_detail_audit import contrast


def test_local_gray_difference_ignores_other_annotated_regions():
    image = np.full((48, 48), 110, dtype=np.uint8)
    foreground = np.zeros_like(image, dtype=bool)
    other = foreground.copy()
    foreground[20:24, 19:28] = True
    other[14:16, 19:28] = True
    image[foreground] = 95
    image[other] = 70

    assert contrast(image, foreground, foreground | other) == 15
