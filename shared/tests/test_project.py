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


if __name__ == '__main__':
    unittest.main()
