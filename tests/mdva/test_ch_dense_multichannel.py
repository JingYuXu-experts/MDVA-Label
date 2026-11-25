import numpy as np

from mdva.training.ch_dense_multichannel import phenotype, shared_features


def test_triplet_features_preserve_the_single_channel_control_and_expose_invalid_pixels():
    visible = np.full((512, 512), 30.)
    extra = visible.copy()
    extra[242:254, 250:258] = 70
    extra[200, 200] = np.nan
    features, support = shared_features({'193': visible, '171': extra, '211': visible})

    assert features.shape == (512, 512, 8)
    assert np.isfinite(features).all()
    assert support[246, 254] and not support[200, 200] and not support[0, 0]
    assert features[246, 254, 4] > features[260, 254, 4]
    assert np.isclose(features[246, 254, 0], features[260, 254, 0])


def test_core_score_and_whole_disk_score_keep_separate_denominators():
    truth = np.zeros((32, 32), dtype=bool)
    truth[4:7, 4:7] = True
    truth[22:28, 22:28] = True
    support = np.zeros_like(truth)
    support[:16] = True

    result = phenotype(truth & support, truth, support)

    assert result['pixels']['dice'] == 1
    assert result['within_support'] == {'matched': 1, 'reference': 1, 'predicted': 1}
    assert result['whole_disk'] == {'matched': 1, 'reference': 2, 'predicted': 1}
