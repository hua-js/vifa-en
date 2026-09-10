import copy
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from pathlib import Path
import tempfile

from test_pv_forecast_tools import load
from test_open_meteo_forecast import snapshot


def archive_snapshot():
    _, response = snapshot()
    for field in ('current', 'current_units'):
        response.pop(field)
    start = datetime.fromisoformat('2026-09-01T00:00:00')
    response['hourly'] = {'time': [(start+timedelta(hours=i)).isoformat(timespec='minutes') for i in range(24)],
        **{name: [20.0]*24 for name in response['hourly_units'] if name != 'time'}}
    module = load('fetch-open-meteo-history.py')
    envelope = {'version': 1, 'es_sn': 'ES02', 'endpoint': module.ENDPOINT,
        'parameters': module.parameters('2026-09-01', '2026-09-01'),
        'request_started_at': '2026-09-01T10:05:00+08:00',
        'received_at': '2026-09-01T10:05:01.123456+08:00', 'fetched_at': '2026-09-01T10:05:01.124000+08:00'}
    return envelope, response


def prepare(envelope, response):
    raw = json.dumps(response).encode()
    envelope = {**envelope, 'response_sha256': hashlib.sha256(raw).hexdigest()}
    return load('fetch-open-meteo-history.py').prepare_snapshot(envelope, raw)


class ArchiveTests(unittest.TestCase):
    def test_only_ended_hours_are_stored_with_true_receipt_and_replay_identity(self):
        envelope, response = archive_snapshot()
        rows, summary = prepare(envelope, response)
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[-1]['weather_time'], '2026-09-01T10:00:00+08:00')
        self.assertEqual(summary['excluded_future_hours'], 13)
        self.assertEqual(rows[0]['source_kind'], 'historical_reanalysis')
        self.assertEqual(rows[0]['fetched_at'], envelope['fetched_at'])
        self.assertIsNone(rows[0]['issued_at'])
        self.assertEqual(prepare(envelope, response)[0], rows)

    def test_missing_values_stay_null_and_incomplete(self):
        envelope, response = archive_snapshot()
        response['hourly']['shortwave_radiation'][2] = None
        rows, summary = prepare(envelope, response)
        self.assertIsNone(rows[2]['ghi_wm2'])
        self.assertEqual(rows[2]['quality_status'], 'incomplete')
        self.assertEqual(summary['incomplete_hourly_rows'], 1)

    def test_gaps_units_request_chronology_and_foreign_grid_are_rejected(self):
        for case in ('gap', 'unit', 'future_request', 'grid', 'precision'):
            envelope, response = archive_snapshot()
            if case == 'gap': response['hourly']['time'][3] = response['hourly']['time'][2]
            if case == 'unit': response['hourly_units']['wind_speed_10m'] = 'm/s'
            if case == 'future_request': envelope['parameters']['end_date'] = '2026-09-02'
            if case == 'grid': response['latitude'] = 40
            if case == 'precision': response['hourly']['temperature_2m'][0] = 20.12345
            with self.subTest(case=case), self.assertRaises(ValueError): prepare(envelope, response)


class IncrementalPVTests(unittest.TestCase):
    def test_actual_source_has_next_pagination_without_count(self):
        module=load('refresh-pv-training.py');client=module.PVSource('test-credential-placeholder',page_size=2)
        row=lambda hour: dict(es_sn='ES02',timestamp=f'2026-09-09T{hour}:00:00+08:00',ac_solar_power=20)
        client.request_page=lambda since,until,page: {'data':[row('09'),row('10')] if page==1 else [row('11')],
            'meta':{'hasNext':page==1,'page':page,'pageSize':2}}
        rows=client.fetch('2026-09-09T00:00:00+08:00','2026-09-09T12:00:00+08:00')
        self.assertEqual(len(rows),3)
        client.request_page=lambda *a: {'data':[row('09')],'meta':{'hasNext':True,'page':1,'pageSize':2}}
        with self.assertRaises(ValueError):client.fetch('2026-09-09T00:00:00+08:00','2026-09-09T12:00:00+08:00')

    def test_fixed_upper_bound_pagination_rejects_a_changing_count(self):
        module=load('refresh-pv-training.py')
        client=module.PVSource('test-credential-placeholder', page_size=2)
        row=lambda hour: dict(es_sn='ES02',timestamp=f'2026-09-09T{hour}:00:00+08:00',ac_solar_power=20)
        client.request_page=lambda since,until,page: {'data':[row('09'),row('10')] if page==1 else [row('11')],
            'meta':{'page':page,'count':3 if page==1 else 4,'totalPage':2}}
        with self.assertRaises(ValueError): client.fetch('2026-09-09T00:00:00+08:00','2026-09-09T12:00:00+08:00')

    def test_loader_preserves_weather_batch_lineage_and_rejects_modified_source(self):
        module=load('refresh-pv-training.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[dict(es_sn='ES02',timestamp='2026-09-08T10:00:00+08:00',ac_solar_power=20)]
            weather=[]
            for hour in ('10','11'):
                weather.append(dict(es_sn='ES02',source_kind='historical_reanalysis',source_batch_id='b'*64,
                    weather_time=f'2026-09-08T{hour}:00:00+08:00',fetched_at='2026-09-09T10:00:00+08:00',
                    content_hash='c'*64,**{f:20.0 for f in module.pv_training.WEATHER_FIELDS}))
            module.save_source(root/'source',rows,weather,{'observed_at':'2026-09-09T12:00:00+08:00'})
            data=module.load_source(root/'source')
            self.assertEqual(data['weather_batches'],['b'*64])
            (root/'source/pv_snapshot.jsonl').write_text('changed')
            with self.assertRaises(ValueError): module.load_source(root/'source')

    def test_recent_window_is_replaced_while_long_history_is_preserved(self):
        module = load('refresh-pv-training.py')
        old = [dict(es_sn='ES02', timestamp='2026-08-18T10:00:00+08:00', ac_solar_power=10),
               dict(es_sn='ES02', timestamp='2026-09-08T10:00:00+08:00', ac_solar_power=None)]
        new = [dict(es_sn='ES02', timestamp='2026-09-08T10:00:00+08:00', ac_solar_power=20),
               dict(es_sn='ES02', timestamp='2026-09-09T10:00:00+08:00', ac_solar_power=30)]
        result = module.merge_pv(old, new, '2026-09-07T12:00:00+08:00', '2026-09-09T12:00:00+08:00')
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]['ac_solar_power'], 10)
        self.assertEqual(result[1]['ac_solar_power'], 20)

    def test_duplicate_wrong_station_or_out_of_window_delta_rejected(self):
        module = load('refresh-pv-training.py')
        row = dict(es_sn='ES02', timestamp='2026-09-08T10:00:00+08:00', ac_solar_power=20)
        for rows in ([row, row], [{**row, 'es_sn': 'ES01'}], [{**row, 'timestamp': '2026-09-10T10:00:00+08:00'}]):
            with self.assertRaises(ValueError):
                module.merge_pv([], rows, '2026-09-07T12:00:00+08:00', '2026-09-09T12:00:00+08:00')


if __name__ == '__main__': unittest.main()
