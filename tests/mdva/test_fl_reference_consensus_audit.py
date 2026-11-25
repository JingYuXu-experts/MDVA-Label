import numpy as np

from mdva.training.fl_reference_consensus_audit import corroboration


def test_corroborated_regions_do_not_absorb_disjoint_annotations():
    first = np.zeros((64, 64), dtype=bool)
    second = np.zeros_like(first)
    first[4:12, 4:12] = second[4:12, 4:12] = True
    first[24:30, 24:30] = True
    second[40:46, 40:46] = True

    _, best, dice = corroboration([first, second])

    assert all(row.tolist() == [1., 0.] for row in best)
    assert dice == [64 / 100]
