import openpyxl

from mdva.web.deps import Workspaces
from mdva.web.solar_catalog import SolarCatalog, observation_time, instrument_for


def test_tai_and_instrument_are_not_silently_hmi():
    day,clock=observation_time('hmi.m_720s.20120101_000000_TAI.1.magnetogram.jpg')
    assert day=='2011-12-31' and clock=='235926'
    assert instrument_for('AR/AR_image/mdi.magnetogram.jpg')=='MDI 磁图'
    assert observation_time('HrHa1005010527fullc.jpg')==('2010-05-01','0527')


def test_missing_excel_clock_does_not_abort_index(tmp_path):
    source=tmp_path/'source';source.mkdir()
    book=openpyxl.Workbook();sheet=book.active
    sheet.append(['STARTHOUR','STARTMIN','DAY','ENDHOUR',None,'ENDMIN',None,'PEAKHOUR','PEAKMIN','CLASS','NOAA_NUM'])
    sheet.append(['//','//',20261111,12,None,30,None,12,15,'C1',1234])
    book.save(source/'flare list.xlsx');book.close()
    workspace=tmp_path/'workspace';workspace.mkdir()
    ws=Workspaces(workspace,sources={'solar_forecast':source})
    try:
        catalog=SolarCatalog(ws.hub());catalog.index()
        assert catalog.status()['events']==1
        assert catalog.status()['last_import']['invalid_event_time_fields']==1
    finally:ws.close()
