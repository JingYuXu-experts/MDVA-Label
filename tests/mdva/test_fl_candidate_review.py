import numpy as np

from mdva.training.fl_candidate_review import (
    FEATURES,
    apply_review,
    describe,
    votes,
)


def test_describe_has_stable_region_order_and_measures_dark_local_contrast():
    gray = np.full((512, 512), .5, dtype=np.float32)
    scores = np.zeros_like(gray)
    scores[200:205, 180:201] = .9
    gray[200:205, 180:201] = .2

    raster, inputs = describe(gray, scores)

    assert raster.max() == 1 and inputs.shape == (1, len(FEATURES))
    assert np.isclose(inputs[0, FEATURES.index('rim_minus_inside')], .3)
    assert np.isclose(inputs[0, FEATURES.index('mean_score')], .9)


def test_reviewer_preserves_reference_association_and_counts_annotation_disagreement():
    row = {
        'features': [[0.] * len(FEATURES), [1.] * len(FEATURES)],
        'geometry': [
            {'reference_ids': [1], 'reference_sizes': [35], 'iou': [[0.], [.9]]},
            {'reference_ids': [1], 'reference_sizes': [35], 'iou': [[.8], [0.]]},
        ],
    }
    model = {'mean': [0.] * len(FEATURES), 'scale': [1.] * len(FEATURES),
             'weights': [1.] * len(FEATURES), 'intercept': 0.}
    assert votes(row).tolist() == [1, 1]

    reviewed = apply_review(row, model, {'probability': .5, 'shape_and_brightness': .5})

    assert [g['matched'] for g in reviewed['review']['original']] == [1, 1]
    assert [g['matched'] for g in reviewed['review']['shape_and_brightness']] == [1, 0]
    assert [g['predicted'] for g in reviewed['review']['shape_and_brightness']] == [1, 1]
