"""M4 consumes the existing M3 rolling table; no forecasting jobs are invoked."""
import copy
import unittest
from datetime import timedelta

from m4_settings.forecast_source import load_forecast
from test_m4_forecast_source import Client as ManualClient, DAY, START, run


def rolling(begin=START-timedelta(minutes=15), *, station='ES01', status='ok'):
    return dict(station_id=station, as_of=(begin+timedelta(minutes=2)).isoformat(),
        generated_at=(begin+timedelta(minutes=3)).isoformat(), source_data_end=begin.isoformat(),
        status=status, content_hash='a'*64, series_payload=[dict(unique_id='station_total_load',
        unit='kW', model_name='SeasonalNaive', status=status,
        points=[dict(data_time=(begin+timedelta(minutes=15*i)).isoformat(),
            target_time=(begin+timedelta(minutes=15*(i+1))).isoformat(), horizon_step=i+1,
            raw_forecast=1000.0+i, forecast_value=1000.0+i, is_clipped=False) for i in range(96)])])


class Client(ManualClient):
    def __init__(self, latest=None, runs=None):
        super().__init__(runs or [])
        self.latest = [rolling()] if latest is None else latest

    def list_rows(self, table, **kwargs):
        if table == 'energy_forecast_latest':
            self.calls.append((table, kwargs))
            return copy.deepcopy(self.latest)
        return super().list_rows(table, **kwargs)


class RollingForecastTests(unittest.TestCase):
    def fetch(self, client, start=START, now=START):
        return load_forecast(client, 'station-1', plan_start_at=start, now=now)

    def test_rolling_interval_start_is_not_target_end_and_window_ends_at_95_points(self):
        data = self.fetch(Client())
        self.assertEqual(data['coverage_points'], 95)
        self.assertEqual(data['values'][:2], [1001, 1002])
        self.assertEqual(data['values'][-1], 1095)
        self.assertEqual(len(data['values']), 95)
        self.assertIsNone(data['first_missing_at'])
        self.assertFalse(data['issues'])

    def test_only_uncovered_tail_uses_existing_manual_forecast_with_provenance(self):
        client = Client(latest=[rolling(START-timedelta(minutes=30))],runs=[run(1,DAY), run(2,DAY+timedelta(days=1))])
        data = self.fetch(client)
        self.assertEqual(data['values'][:2], [1002,1003])
        self.assertEqual(data['values'][-1], 200+71)
        self.assertEqual(data['coverage_points'],96)
        self.assertEqual(data['rolling_points'],94)
        self.assertEqual(data['manual_points'],2)
        self.assertEqual(data['source_kind'],'rolling_with_manual')
        self.assertEqual(data['point_sources'][0]['kind'],'rolling')
        self.assertEqual(data['point_sources'][-1],{'kind':'manual','run_id':'run-2'})
        self.assertIn('手动', ' '.join(data['warnings']))
        # The older calendar day is fully superseded and must not be queried.
        point_calls=[kwargs for table,kwargs in client.calls if table=='energy_forecast_manual_points']
        self.assertEqual(len(point_calls),1)
        self.assertEqual(point_calls[0]['filters']['$and'][0]['run_pk']['$eq'],2)

    def test_complete_rolling_window_does_not_depend_on_manual_tables(self):
        client=Client()
        start=START-timedelta(minutes=15)
        data=self.fetch(client,start=start)
        self.assertEqual(data['coverage_points'],96)
        self.assertEqual(data['source_kind'],'rolling')
        self.assertEqual([table for table,_ in client.calls],['energy_forecast_latest'])

    def test_absent_or_out_of_window_rolling_is_explicit_manual_compatibility(self):
        for latest in ([], [rolling(START-timedelta(days=2))]):
            data=self.fetch(Client(latest=latest,runs=[run(1,DAY),run(2,DAY+timedelta(days=1))]))
            self.assertEqual(data['coverage_points'],96)
            self.assertEqual(data['source_kind'],'manual')
            self.assertEqual(data['rolling_points'],0)
            self.assertTrue(data['warnings'])

    def test_warming_and_degraded_are_visible(self):
        for state in ('warming_up','degraded'):
            row=rolling(status=state)
            if state=='degraded':row['series_payload'][0]['fallback_reason']='model_unavailable'
            data=self.fetch(Client(latest=[row]))
            self.assertEqual(data['rolling_status'],state)
            self.assertTrue(any(state in item for item in data['warnings']))

    def test_malformed_rolling_never_silently_uses_manual(self):
        mutations=[lambda r:r.update(station_id='ES02'),
            lambda r:r.update(status='failed'),
            lambda r:r.update(generated_at=(START+timedelta(hours=1)).isoformat()),
            lambda r:r.update(as_of=(START+timedelta(hours=1)).isoformat()),
            lambda r:r.update(source_data_end=(START-timedelta(minutes=30)).isoformat()),
            lambda r:r.update(series_payload='[]'),
            lambda r:r['series_payload'][0].update(unit='%'),
            lambda r:r['series_payload'][0].update(status='error'),
            lambda r:r['series_payload'].append(copy.deepcopy(r['series_payload'][0])),
            lambda r:r['series_payload'][0]['points'].pop(),
            lambda r:r['series_payload'][0]['points'][0].update(horizon_step=True),
            lambda r:r['series_payload'][0]['points'][1].update(data_time=r['source_data_end']),
            lambda r:r['series_payload'][0]['points'][0].update(target_time=r['source_data_end']),
            lambda r:r['series_payload'][0]['points'][0].update(forecast_value=-1),
            lambda r:r['series_payload'][0]['points'][0].update(forecast_value=True),
            lambda r:r['series_payload'][0]['points'][0].update(forecast_value='NaN')]
        for mutate in mutations:
            row=rolling();mutate(row)
            client=Client(latest=[row],runs=[run(1,DAY),run(2,DAY+timedelta(days=1))])
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):self.fetch(client)
            self.assertFalse(any('manual' in table for table,_ in client.calls))
        with self.assertRaises(ValueError):self.fetch(Client(latest=[rolling(),rolling()]))

    def test_version_binds_rolling_batch_values_and_provenance(self):
        original=self.fetch(Client())
        row=rolling();row['series_payload'][0]['points'][1]['forecast_value']+=1
        changed=self.fetch(Client(latest=[row]))
        self.assertNotEqual(original['version'],changed['version'])
        row=rolling();row['content_hash']='b'*64
        self.assertNotEqual(original['version'],self.fetch(Client(latest=[row]))['version'])

    def test_next_day_alignment_and_station_filter(self):
        start=DAY+timedelta(days=1)
        client=Client(latest=[rolling(start-timedelta(minutes=15))])
        data=self.fetch(client,start=start,now=start)
        self.assertEqual(data['values'][0],1001)
        self.assertEqual(client.calls[0][1]['filters'],{'station_id':{'$eq':'ES01'}})


if __name__=='__main__':unittest.main()
