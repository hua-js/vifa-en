import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from m4.settings.daily_inputs import DailyInputService, SHANGHAI


class CurrentSocTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, 15, tzinfo=SHANGHAI)
        self.client = Mock()
        self.service = DailyInputService(SimpleNamespace(client=self.client))

    def sample(self, **changes):
        row = dict(es_sn='ES01', timestamp=self.now.isoformat(), emus_soc='42.5')
        row.update(changes)
        self.client.list_rows.return_value = [row]
        return self.service._current_soc('station-1', self.now)

    def test_latest_station_sample(self):
        result = self.sample()
        self.assertEqual(result['soc_pct'], 42.5)
        self.assertEqual(result['observed_at'], self.now.isoformat())
        self.assertEqual(self.client.list_rows.call_args.kwargs['sort'], '-timestamp')
        self.assertEqual(self.sample(emus_soc=0)['soc_pct'], 0)

    def test_invalid_samples_are_missing_not_zero(self):
        for change in [dict(es_sn='ES02'), dict(emus_soc=None), dict(emus_soc=True),
                       dict(emus_soc='nan'), dict(emus_soc=101),
                       dict(timestamp=(self.now+timedelta(minutes=1)).isoformat()),
                       dict(timestamp=(self.now-timedelta(days=2)).isoformat()),
                       dict(timestamp='2026-09-12T15:00:00')]:
            with self.subTest(change=change):
                self.assertIsNone(self.sample(**change)['soc_pct'])

    def test_no_sample(self):
        self.client.list_rows.return_value = []
        self.assertIsNone(self.service._current_soc('station-1', self.now)['soc_pct'])

    def test_station_power_requires_complete_roster_and_valid_samples(self):
        rows=[dict(f_es_sn='ES01',emu_sn=sn,latest_power=v,last_time_iso=self.now.isoformat())
              for sn,v in [('emu11',-20),('emu12',-30)]]
        self.client.list_rows.return_value=rows
        self.assertEqual(self.service._current_power('station-1',self.now)['power_kw'],-50)
        for invalid in [rows[:1], [rows[0],rows[0]],
                        [rows[0],{**rows[1],'f_es_sn':'ES02'}],
                        [rows[0],{**rows[1],'latest_power':None}],
                        [rows[0],{**rows[1],'last_time_iso':(self.now-timedelta(days=2)).isoformat()}]]:
            self.client.list_rows.return_value=invalid
            self.assertIsNone(self.service._current_power('station-1',self.now)['power_kw'])

    def test_station_two_power_excludes_pv_meter(self):
        self.client.list_rows.return_value=[dict(f_es_sn='ES02',emu_sn='emu2'+str(i),
            latest_power=10,last_time_iso=self.now.isoformat()) for i in range(1,7)]
        self.assertEqual(self.service._current_power('station-2',self.now)['power_kw'],60)
        self.client.list_rows.return_value.append(dict(f_es_sn='ES02',emu_sn='emu27',latest_power=100,last_time_iso=self.now.isoformat()))
        self.assertIsNone(self.service._current_power('station-2',self.now)['power_kw'])
