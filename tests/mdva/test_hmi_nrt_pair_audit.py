import numpy as np

from mdva.training.hmi_nrt_pair_audit import differences, path_identity


def test_field_difference_counts_only_jointly_supported_pixels():
    result = differences(np.array([[20.,100.,300.,500.]]), np.array([[-20.,101.,np.nan,500.]]),
                         np.array([[True,True,True,False]]))

    assert result['shared_pixels'] == 2
    assert result['defined_field_bands']['20']['same_sign'] == .5
    assert result['defined_field_bands']['100']['same_sign'] == 1
    assert result['defined_field_bands']['300']['same_sign'] is None


def test_archive_paths_keep_nrt_and_definitive_series_distinct():
    nrt, archive = path_identity('nrt','20250201_000000')
    definitive, jsoc = path_identity('definitive','20250201_000000')

    assert 'M_720s_nrt' in nrt and 'M_720s_nrt' not in definitive
    assert archive.endswith('2025/02/'+nrt) and jsoc.endswith('2025/02/01/'+definitive)
