import numpy as np

from mdva.training.fl_local_display import display_variants, planes_from_gray, radial_map, tone_map


def test_tone_mapping_preserves_the_brightness_order():
    original = np.linspace(.3,.55,400,dtype=np.float32).reshape(20,20)
    valid = np.ones((20,20),dtype=bool)
    mapped = tone_map(original,valid,np.linspace(.1,.9,99))
    assert np.min(np.diff(mapped.ravel())) >= 0
    assert np.ptp(mapped) > np.ptp(original)


def test_radial_correction_flattens_the_disk_and_preserves_its_support():
    radius = radial_map((128,128),54.)
    support = radius <= 1.03
    gray = np.clip(.55-.22*radius,0.,1.).astype(np.float32)
    original = planes_from_gray(gray,support)
    fields = display_variants(original,support,27,np.linspace(.1,.9,99))
    assert fields['raw'] is original
    for corrected in fields.values():
        assert np.isfinite(corrected).all()
        assert (corrected[:,~support] == 0).all()
    middle = (radius>=.15)&(radius<=.80)
    assert fields['radial'][0,middle].std() < original[0,middle].std()*.25
