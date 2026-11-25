from mdva.training.forecast_sharp_nrt_join import attach, index_catalog


STAMP = '2021.01.01_00:00:00_TAI'


def test_same_region_catalog_respects_unique_ids_and_issue_time():
    fields={'HARPNUM':['51'],'NOAA_ARS':['12805'],'T_REC':[STAMP],'QUALITY':['0x00000400'],
            'DATE':['2021-01-01T01:00:00Z'],'USFLUX':['1e20'],'AREA_ACR':['10'],'MEANGBT':['22'],'R_VALUE':['3.1']}
    data={'status':0,'count':1,'keywords':[{'name':key,'values':v} for key,v in fields.items()]}
    eligible={'target':1,'excluded_by_split_purge':False,'observed_utc':'2020-12-31T23:59:23+00:00',
              'issue_utc':'2021-01-01T01:59:23+00:00','features':{'base':2.},'noaa':12805}
    row = index_catalog(data,STAMP)[12805]
    attached = attach(eligible,row,STAMP)
    assert attached['state']=='matched_generated_before_cutoff'
    assert attached['features']['nrt_log_usflux']==20
    assert not attached['actual_historical_receipt_verified']
    assert attach({**eligible,'issue_utc':'2021-01-01T00:59:23+00:00'},row,STAMP)['state']=='generated_after_cutoff_or_invalid'
    fields['NOAA_ARS'][0]='12805,12806'
    multiple = index_catalog(data,STAMP)[12805]
    assert attach(eligible,multiple,STAMP)['state']=='ambiguous_or_multi_region_patch'
