from datetime import date

from mdva.training.fl_swefil_review import samples


def test_swefil_uses_date_isolated_raw_frames_and_excludes_sunspots_from_target():
    prefix = 'images/20200520010203Mh'
    previous = 'images/20190101010203Bh_raw.jpg'
    images = [{'file_name':name,'id':i,'width':2048,'height':2048}
              for i,name in enumerate((prefix+'_raw.jpg',prefix+'.jpg',previous))]
    body = {'categories':[{'id':i,'name':name} for i,name in enumerate(('QRF','IRF','ARF','SUNSPOT'))],
            'images':images,'annotations':[{'image_id':0,'category_id':0},{'image_id':0,'category_id':3}]}
    tree = [{'path':row['file_name'],'lfs':{'oid':'a'*64}} for row in images]
    selected, labels = samples(body,tree,{date(2019,1,1)})
    assert [r['file'] for r in selected] == [prefix+'_raw.jpg']
    assert selected[0]['label_counts'] == {'QRF':1,'SUNSPOT':1}
    assert labels[0] == [{'image_id':0,'category_id':0}]
