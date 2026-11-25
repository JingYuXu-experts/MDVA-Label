import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.forecast_region_texture import MASKED, TEXTURE, texture


def test_region_texture_only_uses_the_predicted_support_inside_the_disk():
    values = np.full((32, 32), 128, dtype=np.uint8)
    values[11:20, 12:16] = 40
    values[11:20, 16:21] = 220
    core = np.ones_like(values, dtype=bool)
    result = texture(values, core, core)
    assert result[TEXTURE[0]] > 0 and result[TEXTURE[1]] > 0
    for plain, masked in zip(TEXTURE, MASKED, strict=True):
        assert result[plain] == result[masked]
    no_candidate = texture(values, core, ~core)
    assert no_candidate[MASKED[0]] == no_candidate[MASKED[1]] == 0
    np.testing.assert_equal([result[key] for key in TEXTURE], [no_candidate[key] for key in TEXTURE])
