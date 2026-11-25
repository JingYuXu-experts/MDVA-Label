import pytest
from mdva.training.scss_cases import select


def test_fixed_baseline_quantiles_and_both_error_tails_preserved():
    rows=[{'uri':f'{year}-{i:02d}','year':year,'control_dice':i/10,'new_dice':(9-i)/10} for year in ('2019','2020') for i in range(10)]
    chosen=select(rows);uris={v['row']['uri'] for v in chosen}
    assert all(f'{year}-{i:02d}' in uris for year in ('2019','2020') for i in (1,4,8))
    assert any('退步' in reason for c in chosen for reason in c['reasons'])
    assert any('改善' in reason for c in chosen for reason in c['reasons'])
    assert len(uris)==len(chosen) and chosen==select(list(reversed(rows)))


def test_case_selection_rejects_duplicate_or_empty_images():
    with pytest.raises(ValueError):select([])
    with pytest.raises(ValueError):select([{'uri':'same'},{'uri':'same'}])
