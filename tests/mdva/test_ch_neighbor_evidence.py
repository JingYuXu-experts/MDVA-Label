import numpy as np

from mdva.training.ch_neighbor_evidence import coincident, local_shift, recount


def test_radius_rounding_is_allowed_only_if_placement_and_crop_are_identical():
    basic = {'grid':[512.5,512.5,2.4,2.4,0.,0.,0.,'HPLN-TAN','HPLT-TAN',948.64415],
             'observed_utc':'2013-08-21T01:00:01+00:00'}
    other = {'grid':basic['grid'][:-1]+[948.644258],
             'observed_utc':'2013-08-21T01:00:07+00:00'}
    assert coincident(basic,other)
    other['grid'][0] += .2
    assert not coincident(basic,other)


def test_texture_registration_estimates_shift_without_using_candidate_pixels():
    rng = np.random.default_rng(2026)
    bg = rng.uniform(.5,2.,(512,512))
    mask = np.zeros(bg.shape,dtype=bool)
    mask[210:270,220:280] = True
    source = bg.copy()
    source[mask] = .1
    observed = np.roll(bg,(1,-4),(0,1))
    result = local_shift(source,observed,mask)
    assert result['state'] == 'registered'
    assert result['offset_yx'] == [1,-4]


def test_retention_audit_counts_supported_reference_matches_only():
    case = {'channels':['193','211','hmi'],'known_reference_metrics':{'matching_regions':[{'candidate':1}]},
            'candidates':[{'candidate':1,'spectral_state':'coincident_darkness','field_state':'unavailable'},
                          {'candidate':2,'spectral_state':'coincident_darkness','field_state':'dominant_polarity'}]}
    assert recount([case]) == {'matched':1,'with_magnetogram':1,'verified_spatial_support':1,'would_retain':0}
