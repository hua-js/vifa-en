"""95-point scheduling keeps an exact contiguous window and physical boundaries."""
import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

from m4.optimizer import M4Optimizer
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.model import build_model
from m4.optimizer.validation import validate_candidate
from m4.settings.forecast_source import load_forecast
from m4.settings.live_inputs import LiveInputService, request_from_inputs
from m4.settings.peak_preparation import summarize_peak_preparation
from m4.tests.m4_optimizer_test_support import make_request, make_profiles
from m4.tests.test_m4_live_inputs import Client, Controls, NOW, START
from m4.tests.test_m4_realtime import configuration
from m4.tests.test_m4_rolling_forecast_source import rolling


class VariableHorizonTests(unittest.TestCase):
    def bundle(self, station='station-1', shift=0):
        client=Client(station_id=station)
        client.rolling=[rolling(START-timedelta(minutes=15+shift),station=client.station)]
        client.runs=[]
        config=configuration().model_copy(update={'station_id':station})
        return client,config,LiveInputService(client,Controls(station)).fetch(config,now=NOW)

    def test_95_contiguous_points_are_ready_for_both_stations_and_all_sources_align(self):
        for station in ('station-1','station-2'):
            with self.subTest(station=station):
                client,config,bundle=self.bundle(station)
                self.assertEqual(bundle['status'],'ready',bundle['issues'])
                self.assertEqual(bundle['horizon_points'],95)
                self.assertEqual(bundle['plan_end_at'],(START+timedelta(minutes=1425)).isoformat())
                for source in ('load','pv','tariff'):
                    self.assertEqual(len(bundle['sources'][source]['values']),95)
                self.assertEqual(len(bundle['sources']['tariff']['period_types']),95)
                request=request_from_inputs(config,bundle,make_profiles(),now=NOW)
                self.assertEqual(request.horizon_points,95)
                self.assertEqual(request.points[-1].timestamp,START+timedelta(minutes=94*15))
                self.assertFalse(any('manual' in table for table,_ in client.calls))

    def test_94_points_still_block_and_do_not_pad_or_shift(self):
        _,config,bundle=self.bundle(shift=15)
        self.assertEqual(bundle['status'],'blocked')
        self.assertEqual(bundle['sources']['load']['coverage_points'],94)
        self.assertEqual(bundle['points'],[])
        with self.assertRaises(ValueError):request_from_inputs(config,bundle,make_profiles(),now=NOW)

    def test_internal_gap_cannot_be_treated_as_95_point_prefix(self):
        client,config,_=self.bundle()
        values=[100.0]*96;values[40]=None
        fake=dict(values=values,coverage_points=95,issues=['中间缺点'],version='broken')
        with patch('m4.settings.live_inputs.load_forecast',return_value=fake):
            bundle=LiveInputService(client,Controls()).fetch(config,now=NOW)
        self.assertEqual(bundle['status'],'blocked')

    def test_request_rejects_tampered_window_or_mixed_source_lengths(self):
        _,config,bundle=self.bundle()
        for change in ('end','horizon','pv','tariff','points'):
            altered=copy.deepcopy(bundle)
            if change=='end':altered['plan_end_at']=(START+timedelta(days=1)).isoformat()
            elif change=='horizon':altered['horizon_points']=96
            elif change=='pv':altered['sources']['pv']['values'].append(0)
            elif change=='tariff':altered['sources']['tariff']['period_types'].append('gu')
            else:altered['points'].pop()
            with self.subTest(change=change),self.assertRaises(ValueError):
                request_from_inputs(config,altered,make_profiles(),now=NOW)

    def test_95_point_model_solves_and_terminal_soc_and_cost_use_actual_end(self):
        original=make_request()
        payload=original.model_dump();payload.update(horizon_points=95,points=payload['points'][:95])
        request=OptimizationRequest.model_validate(payload)
        built=build_model(request)
        self.assertEqual(built.index.charge.stop-built.index.charge.start,95)
        self.assertEqual(built.index.energy.stop-built.index.energy.start,96)
        result=M4Optimizer(model_version='test-window').optimize(request)
        for candidate in result.candidates:
            self.assertIn(candidate.status,('optimal','feasible'))
            self.assertEqual(len(candidate.plan),95)
            validate_candidate(request,candidate)
            cost=sum((p.grid_import_kw*f.buy_price_per_kwh-p.grid_export_kw*f.sell_price_per_kwh)*.25
                     for p,f in zip(candidate.plan,request.points))
            self.assertAlmostEqual(candidate.metrics.energy_cost,cost,places=5)
            self.assertLessEqual(abs(candidate.plan[-1].expected_soc_pct-request.capability.initial_soc_pct),
                                 request.constraints.terminal_soc_tolerance_pct+1e-4)
            summary=summarize_peak_preparation(request,candidate)
            self.assertEqual(summary['plan_end_at'],(request.plan_start_at+timedelta(minutes=1425)).isoformat())
        for count in (94,97):
            with self.subTest(count=count),self.assertRaises(ValueError):
                OptimizationRequest.model_validate({**payload,'horizon_points':count})

    def test_95_point_valley_labels_do_not_disable_a_fully_known_overnight_block(self):
        from m4.tests.test_m4_optimizer_early_valley import valley_request
        original=valley_request(start_hour=22)
        payload=original.model_dump();payload.update(horizon_points=95,points=payload['points'][:95])
        built=build_model(OptimizationRequest.model_validate(payload))
        self.assertEqual(built.valley_charge_windows,(tuple(range(8,40)),))

    def test_95_point_pv_policy_uses_dynamic_binary_variables(self):
        payload=make_request().model_dump()
        payload.update(horizon_points=95,points=payload['points'][:95],pv_dispatch_policy='load_first_economic')
        for point in payload['points'][40:48]:point['pv_forecast_kw']=150.0
        request=OptimizationRequest.model_validate(payload)
        result=M4Optimizer(model_version='test-window-pv').optimize(request)
        for candidate in result.candidates:
            self.assertIn(candidate.status,('optimal','feasible'))
            validate_candidate(request,candidate)
            self.assertEqual(len(candidate.plan),95)


if __name__=='__main__':unittest.main()
