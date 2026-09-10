import unittest
from datetime import datetime, timedelta
from m4.settings.forecast_source import load_forecast

START=datetime.fromisoformat('2026-09-07T18:00:00+08:00')
DAY=START.replace(hour=0)

def run(identifier,start,completed=None,station='ES01'):
 return dict(id=identifier,run_id=f'run-{identifier}',station_id=station,status='succeeded',
  forecast_start=start.isoformat(),forecast_end=(start+timedelta(days=1)).isoformat(),
  interval_seconds=900,expected_points_per_series=96,completed_at=(completed or DAY-timedelta(hours=1)).isoformat(),content_hash=f'hash-{identifier}')

class Client:
 def __init__(self,runs,points=None):self.runs=runs;self.points=points;self.calls=[]
 def list_rows(self,table,**kwargs):
  self.calls.append((table,kwargs))
  if table=='energy_forecast_latest':return []
  if table=='energy_forecast_manual_runs':return self.runs
  identifier=kwargs['filters']['$and'][0]['run_pk']['$eq']
  item=next(r for r in self.runs if r['id']==identifier)
  if self.points is not None:return self.points
  start=datetime.fromisoformat(item['forecast_start'])
  return [dict(run_pk=identifier,unique_id='station_total_load',target_time=(start+timedelta(minutes=15*i)).isoformat(),horizon_step=i+1,forecast_value=identifier*100+i) for i in range(96)]

class ForecastTests(unittest.TestCase):
 def test_consecutive_runs_cover_rolling_day_and_newest_wins(self):
  client=Client([run(1,DAY),run(2,DAY,DAY+timedelta(hours=1)),run(3,DAY+timedelta(days=1))])
  data=load_forecast(client,'station-1',plan_start_at=START,now=START)
  self.assertEqual(data['coverage_points'],96)
  self.assertEqual(data['values'][0],272)
  self.assertEqual(data['values'][24],300)
  self.assertEqual([r['run_id'] for r in data['runs']],['run-2','run-3'])
 def test_missing_future_is_explicit_not_repeated_today(self):
  data=load_forecast(Client([run(1,DAY)]),'station-1',plan_start_at=START,now=START)
  self.assertEqual(data['coverage_points'],24)
  self.assertTrue(all(v is None for v in data['values'][24:]))
  self.assertTrue(data['issues'])
 def test_station_time_grid_and_success_are_checked(self):
  for change in ({'station_id':'ES02'},{'interval_seconds':300},{'status':'running'}, {'completed_at':(START+timedelta(hours=1)).isoformat()}):
   item=run(1,DAY);item.update(change)
   with self.subTest(change=change):
    data=load_forecast(Client([item]),'station-1',plan_start_at=START,now=START)
    self.assertEqual(data['coverage_points'],0)
 def test_malformed_latest_run_is_not_replaced_with_older_values(self):
  with self.assertRaises(ValueError):
   load_forecast(Client([run(1,DAY)],points=[]),'station-1',plan_start_at=START,now=START)
 def test_unknown_station_and_unaligned_start_fail(self):
  with self.assertRaises(ValueError):load_forecast(Client([]),'other',plan_start_at=START,now=START)
  with self.assertRaises(ValueError):load_forecast(Client([]),'station-1',plan_start_at=START+timedelta(minutes=1),now=START)
 def test_version_tracks_actual_forecast_values(self):
  a=load_forecast(Client([run(1,DAY)]),'station-1',plan_start_at=START,now=START)
  b=load_forecast(Client([run(2,DAY)]),'station-1',plan_start_at=START,now=START)
  self.assertNotEqual(a['version'],b['version'])
 def test_numeric_database_strings_are_normalized_without_changing_version(self):
  points=[dict(run_pk=1,unique_id='station_total_load',target_time=(DAY+timedelta(minutes=15*i)).isoformat(),
               horizon_step=i+1,forecast_value=100.25+i) for i in range(96)]
  numeric=load_forecast(Client([run(1,DAY)],points=points),'station-1',plan_start_at=DAY,now=DAY)
  strings=[{**point,'forecast_value':f"{point['forecast_value']:.6f}"} for point in points]
  parsed=load_forecast(Client([run(1,DAY)],points=strings),'station-1',plan_start_at=DAY,now=DAY)
  self.assertEqual(parsed['coverage_points'],96)
  self.assertEqual(parsed['values'][:3],[100.25,101.25,102.25])
  self.assertEqual(parsed['values'],numeric['values'])
  self.assertEqual(parsed['version'],numeric['version'])
 def test_invalid_numeric_strings_booleans_and_non_finite_power_are_rejected(self):
  points=[dict(run_pk=1,unique_id='station_total_load',target_time=(DAY+timedelta(minutes=15*i)).isoformat(),
               horizon_step=i+1,forecast_value=100.0) for i in range(96)]
  for value in ('',' ',' 1','1 ','NaN','Infinity','-Infinity','1e9999','-1','-1e-9999',
                True,False,None,-1,float('nan'),float('inf')):
   with self.subTest(value=value),self.assertRaises(ValueError):
    changed=[{**points[0],'forecast_value':value},*points[1:]]
    load_forecast(Client([run(1,DAY)],points=changed),'station-1',plan_start_at=DAY,now=DAY)

if __name__=='__main__':unittest.main()
