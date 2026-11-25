import numpy as np

from mdva.training.ch_aux_signal_audit import features, match_control


def test_control_matches_193_brightness_and_geometry_without_reusing_reference():
    y, x = np.where(np.pad(np.ones((8, 8), bool), ((240, 264), (180, 324))))
    taken = np.zeros((512, 512), dtype=bool)
    taken[y, x] = True
    yy, xx = np.ogrid[:512, :512]
    radial = np.hypot(yy - 255.5, xx - 255.5)
    valid = radial < 200
    image = np.full((512, 512), 200.)
    image[y, x] = 100
    image[y, x + 144] = 105

    _, dy, dx, brightness_delta, radius_delta = match_control(y, x, taken, valid, radial, image)

    assert (dy, dx) == (0, 144)
    assert brightness_delta <= .10 and radius_delta <= 16
    channels = {'171': 2 * image, '193': image, '211': .5 * image, 'hmi': -image}
    assert features(channels, y, x) == {'171_over_193': 2, '211_over_193': .5, 'los_sign_imbalance': 1}
