import unittest


class ProjectTests(unittest.TestCase):
    def test_default_inventory_and_public_boundary(self):
        from shared.project import get_project
        project = get_project()
        self.assertEqual(project.id, 'vifa')
        self.assertEqual(project.station('station-2').source_code, 'ES02')
        self.assertEqual(len(project.station('station-2').inverter_sns), 9)
        self.assertEqual(set(project.public_metadata()), {'schema_version', 'project_id', 'stations', 'configuration_version'})
        with self.assertRaises(ValueError):
            project.station('../station-1')


    def test_invalid_configuration_is_rejected(self):
        import copy
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from shared.project import load_project, ROOT
        original = json.loads((ROOT/'config/projects/vifa.json').read_text())
        mutations = [
            lambda d: d.update(token='not-a-real-secret'),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(timezone='UTC'),
            lambda d: d['m4'].update(minimum_dispatch_power_kw=True),
            lambda d: d['m4'].update(unknown_option=1),
            lambda d: d['stations'][1]['m4'].update(telemetry_max_charge_kw=599),
            lambda d: d['stations'][1]['m4'].update(telemetry_soc_upper_exclusive_pct=101),
            lambda d: d['stations'][1]['m4'].update(ems_discharge_kw={'ping': 540}),
            lambda d: d['stations'][0].update(id='../escape'),
            lambda d: d['stations'][1].update(source_code='ES01'),
            lambda d: d['stations'][1].update(cabinet_sns=['emu11']),
            lambda d: d['stations'][0].update(has_pv='false'),
            lambda d: d['stations'][0].update(policy='peak_reserve'),
            lambda d: d['stations'][0].update(grid_import_limit_kw=None),
            lambda d: d['stations'][0].update(grid_import_limit_kw=float('nan')),
            lambda d: d['sources'].update(m4_base_url='https://user:pass@example.invalid/api'),
            lambda d: d['sources'].update(m4_base_url='https://example.invalid/api?token=x'),
            lambda d: d['sources'].update(m4_base_url='https://example.invalid/api#fragment'),
            lambda d: d['sources'].update(m4_base_url='https://example.invalid/%2e%2e/api'),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'project.json'
            for index, change in enumerate(mutations):
                data = copy.deepcopy(original)
                change(data)
                path.write_text(json.dumps(data))
                with self.subTest(case=index), self.assertRaises(ValueError):
                    load_project(path)

    def test_m4_policy_is_project_bound_and_public_fields_are_limited(self):
        from shared.project import get_project
        project = get_project()
        policy = project.station('station-2').m4
        self.assertEqual(policy['telemetry_max_charge_kw'], 650)
        self.assertEqual(policy['telemetry_soc_upper_exclusive_pct'], 99)
        self.assertEqual(policy['ems_charge_kw'], 600)
        public = project.public_metadata()['stations'][1]['m4_display']
        self.assertEqual(public['charge_kw'], 600)
        self.assertNotIn('telemetry_max_charge_kw', public)
        self.assertNotIn('runtime_overrides', public)

    def test_legacy_project_retains_setpoints_without_telemetry_relaxation(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from shared.project import ROOT, load_project
        data = json.loads((ROOT / 'config/projects/vifa.json').read_text())
        data.pop('m4')
        for station in data['stations']:
            station.pop('m4', None)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            path.write_text(json.dumps(data))
            legacy = load_project(path)
            self.assertEqual(legacy.station('station-2').m4['ems_charge_kw'], 600)
            self.assertEqual(legacy.station('station-1').m4['ems_charge_kw'], 100)
            self.assertIsNone(legacy.station('station-2').m4['telemetry_max_charge_kw'])
            self.assertIsNone(legacy.station('station-2').m4['telemetry_soc_upper_exclusive_pct'])
            data['stations'][1]['m4'] = {'telemetry_max_charge_kw': 650}
            path.write_text(json.dumps(data))
            self.assertNotEqual(load_project(path).fingerprint, legacy.fingerprint)


if __name__ == '__main__':
    unittest.main()
