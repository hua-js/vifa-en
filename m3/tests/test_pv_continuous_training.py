"""Incremental training state, failure boundaries and real snapshot merging."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from m3.tests.test_pv_forecast_tools import load
from m3.tests.test_pv_operational import training, weather_rows


class TrainingStateTests(unittest.TestCase):
    def test_lost_state_after_publication_is_not_first_boot(self):
        m = load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); proof = root/'jobs/previous/forecast/publication_verification.json'
            proof.parent.mkdir(parents=True); proof.write_text('{"status":"completed"}')
            (proof.parent.parent/'training-refresh.json').write_text('{"policy":"continuous-training-v1"}')
            with patch.object(m.refresh, 'load_source') as source:
                with self.assertRaises(ValueError):
                    m.refresh_training(root/'jobs/new', root/'seed', root/'training-state.json')
                source.assert_not_called()

    def test_first_boot_uses_seed_and_restart_uses_persisted_snapshot(self):
        m = load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root/'training-state.json'
            legacy = root/'jobs/legacy/forecast/publication_verification.json'
            legacy.parent.mkdir(parents=True); legacy.write_text('{"status":"completed"}')
            with patch.object(m.refresh, 'load_source', return_value={'training': training()}) as source, \
                 patch.object(m.refresh.archive, 'fetch'), patch.object(m.refresh, 'refresh') as update, \
                 patch.object(m.generator, 'load_inputs', return_value=(training(), [], {}, {})):
                # The original fixture is August; use a September tail for archive bounds.
                t = training(); t.index = t.index+pd.Timedelta(days=20); source.return_value={'training': t}
                m.refresh_training(root/'first', root/'seed', state)
                self.assertEqual(source.call_args.args[0], root/'seed')
                state.write_text(json.dumps({'training_source':str(root/'first/training')}))
                m.refresh_training(root/'next', root/'seed', state)
                self.assertEqual(source.call_args.args[0], root/'first/training')
                self.assertFalse(update.call_args.kwargs['require_import'])

    def test_corrupt_state_blocks_before_any_network(self):
        m = load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); state=root/'state.json'; state.write_text('{}')
            with patch.object(m.refresh.archive,'fetch') as fetch:
                with self.assertRaises(ValueError): m.refresh_training(root/'job',root/'seed',state)
                fetch.assert_not_called()

    def test_refresh_failure_prevents_weather_and_publication(self):
        m = load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(m, 'refresh_training', side_effect=ValueError('incomplete refresh')), \
             patch.object(m.collector, 'fetch') as weather, patch.object(m.publisher, 'execute') as publish:
            with self.assertRaises(ValueError): m.perform('forecast', Path(tmp), Path(tmp)/'seed')
            weather.assert_not_called(); publish.assert_not_called()

    def test_state_only_advances_after_verified_publication(self):
        m = load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root/'state.json'; state.write_text('{"training_source":"prior"}')
            def fetch(path): path.mkdir(parents=True); return {}, b'{}'
            def weather(rows, summary, path): path.write_text('{"status":"completed","source_batch_id":"b"}')
            def generate(_, snapshot, bundle, **kwargs):
                bundle.mkdir(); return dict(run_id='new', forecast_start='start', forecast_end='end',
                    training_end='new-end', training_rows=768, forecast_energy_kwh=1, peak_kw=1,
                    peak_time='peak', model_name='WeatherRidge', model_version='v1')
            def publish(bundle, proof): proof.write_text('{"status":"completed","verified_points":96,"run_pk":1}')
            with patch.object(m, 'refresh_training', side_effect=lambda d,*a:d/'training'), \
                 patch.object(m.collector,'fetch',side_effect=fetch), \
                 patch.object(m.collector,'prepare_snapshot',return_value=([{'fetched_at':'now'}],{'source_batch_id':'b'})), \
                 patch.object(m.collector.history,'execute',side_effect=weather), \
                 patch.object(m.generator,'generate',side_effect=generate), \
                 patch.object(m.publisher,'execute',side_effect=ValueError('readback failed')) as pub:
                with self.assertRaises(ValueError): m.perform('forecast',root/'failed',root/'seed',state)
                self.assertEqual(json.loads(state.read_text())['training_source'],'prior')
                pub.side_effect=publish
                m.perform('forecast',root/'success',root/'seed',state)
                self.assertEqual(json.loads(state.read_text())['training_source'],str((root/'success/training').resolve()))


class TrainingMergeTests(unittest.TestCase):
    def test_real_snapshot_refresh_advances_labels_and_preserves_valid_weather(self):
        m = load('refresh-pv-training.py')
        start = pd.Timestamp('2026-09-08T10:00:00+08:00')
        rows = [dict(es_sn='ES02', timestamp=(start+pd.Timedelta(minutes=i)).isoformat(),
            ac_solar_power=20.) for i in range(60)]
        def weather(hour):
            return dict(es_sn='ES02', source_kind='historical_reanalysis', source_batch_id='b'*64,
                weather_time=(start+pd.Timedelta(hours=hour)).isoformat(), fetched_at=None,
                content_hash='c'*64, quality_status='valid', **{f:20. for f in m.pv_training.WEATHER_FIELDS})
        delta = rows+[dict(es_sn='ES02',timestamp=(start+pd.Timedelta(minutes=i)).isoformat(),
            ac_solar_power=30.) for i in range(60,120)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); seed=root/'seed'; archive=root/'archive'; archive.mkdir()
            (archive/'response.json').write_text('{}'); (archive/'envelope.json').write_text('{}')
            m.save_source(seed,rows,[weather(0),weather(1)],{'observed_at':'2026-09-09T12:00:00+08:00'})
            old=m.load_source(seed)
            recent=[dict(weather(1),ghi_wm2=None,quality_status='incomplete'),weather(2)]
            with patch.object(m.archive,'prepare_snapshot',return_value=(recent,{'source_batch_id':'b'*64})), \
                 patch.object(m.history,'credential',return_value='test'), \
                 patch.object(m.PVSource,'fetch',return_value=delta), contextlib.redirect_stdout(io.StringIO()):
                m.refresh(archive,root/'new',seed,require_import=False)
            new=m.load_source(root/'new')
            self.assertEqual(len(old['training']),4); self.assertEqual(len(new['training']),8)
            self.assertEqual(new['training'].index.max(),start+pd.Timedelta(hours=1,minutes=45))
            self.assertEqual(new['weather'][1]['quality_status'],'valid')
            self.assertEqual(m.load_source(seed)['raw'],rows)


class ExpandingReplayTests(unittest.TestCase):
    def test_refit_never_uses_target_day_or_future_labels(self):
        from scripts.replay_pv_training import refit
        original=training()
        added=original.iloc[:2].copy()
        added.index=pd.date_range('2026-09-09',periods=2,freq='15min',tz=original.index.tz)
        added['actual_kw']=999999.
        run=dict(as_of='2026-09-09T22:15:00+08:00',source_manifest=dict(forecast_weather_rows=weather_rows()))
        a, cutoff=refit(original,run); b, _=refit(pd.concat([original,added]),run)
        self.assertEqual(cutoff.isoformat(),'2026-09-08T23:00:00+08:00')
        self.assertEqual(a['model'],b['model'])
        self.assertTrue(a['points'].equals(b['points']))
