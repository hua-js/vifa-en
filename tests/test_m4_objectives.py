import copy
import unittest
from unittest.mock import patch

from m4_optimizer.contracts import OptimizationRequest
from m4_optimizer_test_support import make_request


class ObjectiveSettingsTests(unittest.TestCase):
    def module(self):
        from m4_settings import objectives
        return objectives

    def test_global_defaults_preserve_confirmed_priority_and_core_contract(self):
        module=self.module()
        profiles=module.get_profiles('station-1')
        expected={
            'balanced':[['demand_peak'],['demand_duration'],['soc_preferred_deviation'],
                        ['energy_cost','pv_unused'],['throughput'],['valley_charge_delay']],
            'cost':[['demand_peak'],['demand_duration'],['energy_cost'],['pv_unused'],
                    ['soc_preferred_deviation'],['throughput'],['valley_charge_delay']],
            'pv':[['demand_peak'],['demand_duration'],['pv_unused'],['energy_cost'],
                  ['soc_preferred_deviation'],['throughput'],['valley_charge_delay']],
        }
        self.assertEqual([profile.profile_id for profile in profiles],['balanced','cost','pv'])
        for profile in profiles:
            self.assertEqual([list(layer.terms) for layer in profile.objective_order],expected[profile.profile_id])
            self.assertTrue(all(layer.relative_tolerance==0 for layer in profile.objective_order))
        request=make_request().model_dump()
        request['profiles']=[profile.model_dump() for profile in profiles]
        validated=OptimizationRequest.model_validate(request)
        self.assertEqual(validated.capability,make_request().capability)
        self.assertEqual(validated.constraints,make_request().constraints)

    def test_debug_normalization_is_explicit_and_shared_between_stations(self):
        module=self.module()
        profiles=module.get_profiles('station-1')
        combined=profiles[0].objective_order[3].terms
        self.assertEqual(combined,{'energy_cost':.001,'pv_unused':.001})
        metadata=module.get_profile_metadata('station-1')
        self.assertEqual(metadata['usage'],'candidate_debug')
        self.assertFalse(metadata['production_approved'])
        self.assertIn('调试',metadata['description'])
        self.assertEqual(metadata['normalization']['cost_reference_cny'],1000)
        self.assertEqual(metadata['normalization']['pv_reference_kwh'],1000)
        self.assertEqual(metadata['profile_versions'],{p.profile_id:p.profile_version for p in profiles})
        self.assertEqual(module.get_profiles('station-1'),module.get_profiles('station-2'))
        self.assertEqual(metadata,module.get_profile_metadata('station-2'))

    def test_station_override_is_partial_isolated_and_versioned(self):
        module=self.module()
        global_values={**module.GLOBAL_OBJECTIVE_DEFAULTS,'balanced_pv_reference_kwh':2000.0}
        overrides={'station-2':{'version':'station-2-debug-v2','balanced_cost_weight':2.0}}
        with patch.object(module,'GLOBAL_OBJECTIVE_DEFAULTS',global_values), patch.object(module,'STATION_OBJECTIVE_OVERRIDES',overrides):
            station1=module.get_profiles('station-1')
            station2=module.get_profiles('station-2')
            self.assertEqual(station1[0].objective_order[3].terms,{'energy_cost':.001,'pv_unused':.0005})
            self.assertEqual(station2[0].objective_order[3].terms,{'energy_cost':.002,'pv_unused':.0005})
            self.assertNotEqual(station1[0].profile_version,station2[0].profile_version)
            self.assertEqual(module.get_profile_metadata('station-1')['scope'],'global')
            self.assertEqual(module.get_profile_metadata('station-2')['scope'],'station-2')
            self.assertEqual(overrides,{'station-2':{'version':'station-2-debug-v2','balanced_cost_weight':2.0}})

    def test_changed_configuration_changes_version_even_without_manual_version_bump(self):
        module=self.module()
        before=module.get_profile_metadata('station-1')
        updated={**module.GLOBAL_OBJECTIVE_DEFAULTS,'balanced_cost_reference_cny':500.0}
        with patch.object(module,'GLOBAL_OBJECTIVE_DEFAULTS',updated):
            after=module.get_profile_metadata('station-1')
        self.assertNotEqual(before['configuration_version'],after['configuration_version'])
        self.assertNotEqual(before['profile_versions']['balanced'],after['profile_versions']['balanced'])
        self.assertEqual(before,module.get_profile_metadata('station-1'))

    def test_callers_cannot_mutate_returned_profiles_or_metadata_into_shared_config(self):
        module=self.module()
        before=copy.deepcopy(module.GLOBAL_OBJECTIVE_DEFAULTS)
        profiles=module.get_profiles('station-1')
        metadata=module.get_profile_metadata('station-1')
        profiles[0].objective_order[0].terms.clear()
        profiles.pop()
        metadata['profile_versions'].clear()
        metadata['normalization']['cost_reference_cny']=1
        self.assertEqual(len(module.get_profiles('station-1')),3)
        self.assertEqual(module.get_profiles('station-2')[0].objective_order[0].terms,{'demand_peak':1})
        self.assertEqual(module.get_profile_metadata('station-1')['normalization']['cost_reference_cny'],1000)
        self.assertEqual(module.GLOBAL_OBJECTIVE_DEFAULTS,before)

    def test_invalid_weights_scales_tolerances_and_extra_control_fields_fail_closed(self):
        module=self.module()
        invalid=[
            {'balanced_cost_weight':0}, {'balanced_pv_weight':-1},
            {'balanced_cost_reference_cny':0}, {'balanced_pv_reference_kwh':float('inf')},
            {'absolute_tolerance':-1}, {'relative_tolerance':float('nan')},
            {'balanced_cost_weight':True}, {'version':' '},
            {'demand_limit_kw':99999}, {'soc_min_pct':0},
            {'objective_order':['energy_cost','demand_peak']},
            {'profiles':[]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), patch.object(module,'GLOBAL_OBJECTIVE_DEFAULTS',{**module.GLOBAL_OBJECTIVE_DEFAULTS,**changes}), self.assertRaises(ValueError):
                module.get_profiles('station-1')
        for changes in invalid:
            override={'version':'station-debug-v2',**changes}
            with self.subTest(override=override), patch.object(module,'STATION_OBJECTIVE_OVERRIDES',{'station-2':override}), self.assertRaises(ValueError):
                module.get_profiles('station-2')

    def test_station_override_requires_own_version_and_known_station(self):
        module=self.module()
        with patch.object(module,'STATION_OBJECTIVE_OVERRIDES',{'station-2':{'balanced_cost_weight':2.0}}):
            with self.assertRaises(ValueError):
                module.get_profiles('station-2')
            self.assertEqual(len(module.get_profiles('station-1')),3)
        for station_id in ('ES01','unknown','',None):
            with self.subTest(station_id=station_id), self.assertRaises(ValueError):
                module.get_profiles(station_id)


if __name__=='__main__':
    unittest.main()
