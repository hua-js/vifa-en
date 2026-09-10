import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[2]/'m3/scripts'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load('weather_history_shared_test', 'import-weather-history.py')
forecast = load('open_meteo_collector_test', 'fetch-open-meteo-weather.py')


def snapshot():
    units = {'temperature_2m': '°C', 'relative_humidity_2m': '%', 'cloud_cover': '%',
             'cloud_cover_low': '%', 'cloud_cover_mid': '%', 'cloud_cover_high': '%',
             'shortwave_radiation': 'W/m²', 'direct_radiation': 'W/m²',
             'diffuse_radiation': 'W/m²', 'precipitation': 'mm', 'wind_speed_10m': 'km/h'}
    start = datetime.fromisoformat('2026-09-09T20:00:00')
    response = {'latitude': 23.0, 'longitude': 113.0, 'elevation': 11.0,
        'timezone': 'Asia/Shanghai', 'utc_offset_seconds': 28800, 'generationtime_ms': .2,
        'hourly_units': {'time': 'iso8601', **units},
        'hourly': {'time': [(start+timedelta(hours=i)).isoformat(timespec='minutes') for i in range(50)],
                   **{name: [20.0]*50 for name in units}},
        'current_units': {'time': 'iso8601', 'interval': 'seconds', **units, 'is_day': '', 'weather_code': 'wmo code'},
        'current': {'time': '2026-09-09T21:00', 'interval': 900, **{name: 20.0 for name in units}, 'is_day': 0, 'weather_code': 3}}
    envelope = {'version': 1, 'es_sn': 'ES02', 'endpoint': forecast.ENDPOINT,
        'parameters': forecast.parameters(), 'request_started_at': '2026-09-09T21:11:11+08:00',
        'received_at': '2026-09-09T21:11:12.122666+08:00', 'fetched_at': '2026-09-09T21:11:12.123000+08:00'}
    return envelope, response


def prepare(envelope, response):
    raw = json.dumps(response, ensure_ascii=False).encode()
    envelope = {**envelope, 'response_sha256': hashlib.sha256(raw).hexdigest()}
    return forecast.prepare_snapshot(envelope, raw)


class OpenMeteoCollectorTests(unittest.TestCase):
    def test_one_response_preserves_current_interval_and_hourly_batch_separately(self):
        envelope, response = snapshot()
        rows, summary = prepare(envelope, response)
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]['source_kind'], 'forecast')
        self.assertEqual(rows[0]['interval_minutes'], 60)
        self.assertEqual(rows[0]['weather_time'], '2026-09-09T20:00:00+08:00')
        current = rows[0]['source_metadata']['current_conditions']
        self.assertEqual(current['valid_at'], '2026-09-09T21:00:00+08:00')
        self.assertEqual(current['interval_seconds'], 900)
        self.assertEqual(current['values']['temperature_c'], '20.000')
        self.assertTrue(current['model_based'])
        self.assertIsNone(rows[0]['issued_at'])
        self.assertEqual(rows[0]['fetched_at'], envelope['fetched_at'])
        self.assertTrue(summary['next_24h_weather_complete'])

    def test_replay_same_snapshot_is_identical_but_new_fetch_has_new_batch(self):
        envelope, response = snapshot()
        rows, _ = prepare(envelope, response)
        again, _ = prepare(envelope, response)
        self.assertEqual(rows, again)
        envelope['received_at'] = '2026-09-09T21:12:00+08:00'
        envelope['fetched_at'] = envelope['received_at']
        changed, _ = prepare(envelope, response)
        self.assertNotEqual(rows[0]['source_batch_id'], changed[0]['source_batch_id'])

    def test_bad_units_gaps_duplicate_times_and_nan_fail_before_import(self):
        for change in ('unit', 'gap', 'duplicate', 'nan', 'future_current'):
            with self.subTest(change=change), self.assertRaises(ValueError):
                envelope, response = snapshot()
                if change == 'unit': response['hourly_units']['wind_speed_10m'] = 'm/s'
                if change == 'gap': response['hourly']['time'].pop()
                if change == 'duplicate': response['hourly']['time'][1] = response['hourly']['time'][0]
                if change == 'nan': response['hourly']['shortwave_radiation'][1] = float('nan')
                if change == 'future_current': response['current']['time'] = '2026-09-10T21:00'
                prepare(envelope, response)

    def test_missing_future_values_are_preserved_and_disable_complete_horizon(self):
        envelope, response = snapshot()
        response['hourly']['shortwave_radiation'][4] = None
        rows, summary = prepare(envelope, response)
        self.assertIsNone(rows[4]['ghi_wm2'])
        self.assertEqual(rows[4]['quality_status'], 'incomplete')
        self.assertFalse(summary['next_24h_weather_complete'])

    def test_fetch_time_is_rounded_up_to_database_millisecond_precision(self):
        received = datetime.fromisoformat('2026-09-09T21:11:12.999999+08:00')
        self.assertEqual(forecast.database_time(received).isoformat(), '2026-09-09T21:11:13+08:00')

    def test_modified_raw_response_or_endpoint_is_rejected(self):
        envelope, response = snapshot()
        raw = json.dumps(response).encode()
        envelope['response_sha256'] = '0'*64
        with self.assertRaises(ValueError): forecast.prepare_snapshot(envelope, raw)
        envelope['endpoint'] = 'https://example.org/other'
        with self.assertRaises(ValueError): prepare(envelope, response)


class ForecastImportTimestampTests(unittest.TestCase):
    def test_api_utc_fetch_time_compares_with_explicit_local_time(self):
        row = {name: '0.000' for name in history.DECIMAL_FIELDS}
        row.update(latitude='0.000000', longitude='0.000000')
        row.update(es_sn='ES02', source_kind='forecast', weather_time='2026-09-09T22:00:00+08:00',
                   issued_at=None, fetched_at='2026-09-09T21:11:12.123000+08:00', interval_minutes=60)
        row['content_hash'] = history.digest(row)
        actual = copy.deepcopy(row)
        actual['weather_time'] = '2026-09-09T14:00:00.000Z'
        actual['fetched_at'] = '2026-09-09T13:11:12.123Z'
        self.assertEqual(history.compare_rows([actual], [row]), {row['weather_time']})


if __name__ == '__main__':
    unittest.main()
