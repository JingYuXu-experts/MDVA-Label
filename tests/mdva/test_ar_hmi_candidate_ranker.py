from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('torch')

from mdva.training.ar_hmi_candidate_ranker import block, decision, policy_split
from mdva.training.ar_hmi_small_guard import keep_candidate


def test_ranker_threshold_data_are_later_than_fit_with_whole_block_gap():
    first = datetime(2018, 1, 1, tzinfo=timezone.utc)
    rows = [{'time_utc': (first + timedelta(days=27*i + hour/24)).isoformat()}
            for i in range(16) for hour in (0, 2)]
    fit, policy = policy_split(list(reversed(rows)))
    assert max(block(row['time_utc']) for row in fit) + 1 < min(block(row['time_utc']) for row in policy)
    assert len(fit) + len(policy) < len(rows)


def test_unavailable_magnetogram_never_rejects_an_original_candidate():
    assert decision({'state': 'physical_unavailable'}, {}) == (None, True)
    assert decision({'state': 'local_support_insufficient'}, {}) == (None, True)


def test_ranker_exports_scores_and_applies_the_frozen_retention_threshold():
    model = {'features': ['signed'], 'mean': [1.], 'scale': [2.], 'weights': [3.],
             'intercept': -1., 'review_threshold': .5}
    assert decision({'state': 'measured', 'signed': 2.}, model) == (.5, True)
    assert decision({'state': 'measured', 'signed': 1.}, model) == (-1., False)


def test_small_guard_protects_all_sizes_that_could_match_an_under_256_reference():
    assert keep_candidate(511, -1., 0.)
    assert not keep_candidate(512, -1., 0.)
    assert keep_candidate(800, None, 0.)
