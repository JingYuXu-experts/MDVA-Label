import numpy as np

from mdva.training.fl_sparse_review_analysis import contour_audit, loss_audit
from mdva.training.fl_sparse_reviewer import frame_features, summarize


def test_relative_features_preserve_absolute_scores_and_rank_ties():
    raw = np.zeros((3, 15))
    raw[:, 0] = [.3, .8, .8]
    raw[:, 1] = [.9, .8, .8]
    raw[:, 11] = [.02, .01, .03]
    values = frame_features(raw, relative=True)
    assert np.array_equal(values[:, :15], raw)
    assert np.allclose(values[:, 15], [1 / 6, 2 / 3, 2 / 3])
    assert np.allclose(values[:, 18], [-.5, 0., 0.])
    assert np.allclose(frame_features(raw[:1], relative=True)[0, 15:], [.5, .5, .5, 0., 0., 0.])


def test_blank_reference_versions_keep_predictions_visible_in_report():
    cases = [{'file': 'one', 'frame': {'base_count': 0, 'count': 1,
        'geometry': [{'reference_ids': [1], 'iou': [[.7]]}, {'reference_ids': [], 'iou': [[]]}],
        'scores': {'relative': [.75]}}}]
    row = summarize(cases, {'relative': .5})['relative']
    assert (row['matched'], row['predicted']) == (1, 2)
    assert row['nonempty_output_on_empty_reference_versions'] == row['empty_reference_versions'] == 1


def test_loss_audit_separates_filtering_from_contour_limit():
    cases = [{'file': 'one', 'frame': {'scores': {'balanced': [.9, .3]},
        'geometry': [{'reference_ids': [1, 2, 3], 'iou': [[.7, 0, .4], [0, .6, .2]]}]}}]
    audit = loss_audit(cases, 'balanced', .5)
    assert audit['counts']['matched'] == 1
    assert audit['counts']['qualified_contour_rejected_references'] == 1
    assert audit['counts']['no_qualified_contour_references'] == 1
    assert audit['counts']['review_1_misses_exposed'] == 1


def test_contour_audit_distinguishes_split_support_from_zero_overlap():
    values = np.zeros((2, 15))
    values[:, 3] = np.log1p(30)
    rows = [{'features': values.tolist(), 'standalone': {'geometry': [
        {'reference_sizes': [100, 50], 'iou': [[.3, 0.], [.3, 0.]]}]}}]
    counts = contour_audit(rows)['counts']
    assert counts == {'reference': 2, 'coverage_scattered_across_contours': 1, 'no_overlap': 1}
