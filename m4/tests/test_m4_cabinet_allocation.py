"""Offline cabinet allocation invariants and energy conservation."""
from copy import deepcopy
from datetime import datetime
import unittest
from m4.settings.cabinet_allocation import allocate_cabinets


def fixture():
    return dict(station_id='station-1', date='2026-09-10', now='2026-09-10T19:00:00+08:00',
        configuration_version='cfg', controls_version='ctrl', max_age_seconds=300,
        bounds={'soc_min_pct': 10, 'soc_max_pct': 90}, charge_efficiency=1, discharge_efficiency=1,
        points=[{'power':80 if i==76 else 0} for i in range(96)], snapshot=dict(
        station_id='station-1', configuration_version='cfg', capacity_source_version='ctrl',
        power_limits_source_version='ctrl', available=True, available_max_charge_kw=200,
        available_max_discharge_kw=200, cabinet_power_limits={'max_charge_kw':100,'max_discharge_kw':100},
        cabinets=[dict(emu_sn='emu11',capacity_kwh=100,soc_pct=70,available=True,observed_at='2026-09-10T18:59:59+08:00',issues=[]),
                  dict(emu_sn='emu12',capacity_kwh=100,soc_pct=30,available=True,observed_at='2026-09-10T18:59:59+08:00',issues=[])]))


class CabinetAllocationTests(unittest.TestCase):
    def test_discharge_conserves_energy_and_proportions(self):
        p=allocate_cabinets(fixture())['slots'][0]
        self.assertEqual([r['powerKw'] for r in p['rows']],[60,20])
        self.assertAlmostEqual(p['rows'][0]['endSocPct'],55);self.assertAlmostEqual(p['rows'][1]['endSocPct'],25)
        self.assertEqual(p['shortfallKw'],0)

    def test_charge_efficiency(self):
        x=fixture();x['points'][76]['power']=-80;x['charge_efficiency']=.8
        p=allocate_cabinets(x)['slots'][0]
        self.assertEqual([r['powerKw'] for r in p['rows']],[-20,-60])
        self.assertEqual([r['endSocPct'] for r in p['rows']],[74,42])

    def test_saturation_redistributes(self):
        x=fixture();x['points'][76]['power']=100;x['snapshot']['cabinets'][0]['max_discharge_kw']=20
        self.assertEqual([r['powerKw'] for r in allocate_cabinets(x)['slots'][0]['rows']],[20,80])

    def test_energy_exhaustion_and_next_slot(self):
        x=fixture();x['discharge_efficiency']=.8;x['points'][77]['power']=80
        for c in x['snapshot']['cabinets']:c['soc_pct']=11
        slots=allocate_cabinets(x)['slots'];self.assertAlmostEqual(slots[0]['allocatedKw'],6.4)
        self.assertAlmostEqual(slots[0]['shortfallKw'],73.6);self.assertEqual(slots[1]['allocatedKw'],0)

    def test_unavailable_and_stale(self):
        x=fixture();x['snapshot']['cabinets'][0]['available']=False
        p=allocate_cabinets(x)['slots'][0];self.assertFalse(p['rows'][0]['eligible']);self.assertEqual(p['rows'][1]['powerKw'],80)
        x['snapshot']['cabinets'][1]['observed_at']='2026-09-10T18:00:00+08:00'
        self.assertEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],0)

    def test_current_remaining_time(self):
        x=fixture();x['now']='2026-09-10T19:10:00+08:00'
        for c in x['snapshot']['cabinets']:c.update(soc_pct=11,observed_at='2026-09-10T19:09:59+08:00')
        self.assertAlmostEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],24)

    def test_terminal_reserve(self):
        x=fixture();x.update(reserve_start_index=56,terminal_soc_min_pct=20)
        for c in x['snapshot']['cabinets']:c['soc_pct']=21
        p=allocate_cabinets(x)['slots'][0];self.assertEqual(p['allocatedKw'],8)
        self.assertEqual([r['endSocPct'] for r in p['rows']],[20,20])

    def test_station_cap(self):
        x=fixture();x['snapshot']['available_max_discharge_kw']=40
        self.assertEqual(allocate_cabinets(x)['slots'][0]['shortfallKw'],40)

    def test_identity_version_roster_date_rejected(self):
        changes=[lambda x:x['snapshot'].update(station_id='station-2'),lambda x:x['snapshot'].update(configuration_version='old'),
                 lambda x:x['snapshot'].update(power_limits_source_version='old'),lambda x:x['snapshot']['cabinets'].pop(),
                 lambda x:x.update(now='2026-09-11T00:00:00+08:00'),lambda x:x['points'][80].update(power=float('nan'))]
        for change in changes:
            x=fixture();change(x)
            with self.assertRaises(ValueError):allocate_cabinets(x)

    def test_immutable_input(self):
        x=fixture();before=deepcopy(x);allocate_cabinets(x);self.assertEqual(x,before)

    def test_upper_soc(self):
        x=fixture();x['points'][76]['power']=-80
        for c in x['snapshot']['cabinets']:c['soc_pct']=89
        self.assertEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],-8)

    def test_grid_headroom_and_no_battery_export(self):
        x=fixture();x['grid_import_limit_kw']=100
        for p in x['points']:p.update(load=95,pv=0)
        x['points'][76]['power']=-80;self.assertEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],-5)
        x['points'][76].update(power=80,load=10);self.assertEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],10)

    def test_five_minute_cap(self):
        x=fixture();x['max_age_seconds']=86400
        for c in x['snapshot']['cabinets']:c['observed_at']='2026-09-10T18:54:59+08:00'
        self.assertEqual(allocate_cabinets(x)['slots'][0]['allocatedKw'],0)
