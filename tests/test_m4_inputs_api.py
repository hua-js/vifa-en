import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from m4_settings.api import create_app
from m4_settings.store import SettingsStore
from test_m4_settings import parameters
from unittest.mock import patch

class InputApiTests(unittest.TestCase):
 def test_inputs_receive_server_configuration_and_are_read_only(self):
  class Service:
   def fetch(self,configuration):
    return {'station_id':configuration.station_id,'status':'blocked','configuration_version':configuration.version,'issues':['尚未配置参数']}
  with tempfile.TemporaryDirectory() as temp, TestClient(create_app(Path(temp)/'settings.db',input_service=Service())) as client:
   response=client.get('/m4-api/stations/station-1/inputs')
   self.assertEqual(response.status_code,200)
   self.assertEqual(response.json()['station_id'],'station-1')
   self.assertIsNone(response.json()['configuration_version'])
   self.assertEqual(response.headers['cache-control'],'no-store')
   self.assertEqual(client.post('/m4-api/stations/station-1/inputs',json={}).status_code,405)
   self.assertEqual(client.get('/m4-api/stations/other/inputs').status_code,404)
 def test_unexpected_failure_does_not_expose_exception(self):
  class Service:
   def fetch(self,configuration):raise ValueError('sensitive upstream exception')
  with tempfile.TemporaryDirectory() as temp, TestClient(create_app(Path(temp)/'settings.db',input_service=Service())) as client:
   response=client.get('/m4-api/stations/station-1/inputs')
   self.assertEqual(response.status_code,502)
   self.assertNotIn('sensitive',response.text)
 def test_settings_changed_during_read_cannot_return_ready_inputs(self):
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'settings.db';store=SettingsStore(path)
   initial=store.save('station-1',parameters(),expected_revision=0)
   class Service:
    def fetch(self,configuration):
     store.save('station-1',parameters(max_charge_kw=70),expected_revision=configuration.revision)
     return {'station_id':configuration.station_id,'status':'ready','configuration_version':configuration.version,'issues':[]}
   with TestClient(create_app(path,input_service=Service())) as client:
    response=client.get('/m4-api/stations/station-1/inputs')
    self.assertEqual(response.status_code,409)
    self.assertIn('取数期间已更新',response.json()['detail'])
    self.assertNotIn(initial.version,response.text)
    self.assertEqual(store.get('station-1').revision,2)
 def test_other_station_settings_change_does_not_invalidate_this_station(self):
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'settings.db';store=SettingsStore(path)
   initial=store.save('station-1',parameters(),expected_revision=0)
   class Service:
    def fetch(self,configuration):
     store.save('station-2',parameters(),expected_revision=0)
     return {'station_id':configuration.station_id,'status':'ready','configuration_version':configuration.version,'issues':[]}
   with TestClient(create_app(path,input_service=Service())) as client:
    response=client.get('/m4-api/stations/station-1/inputs')
    self.assertEqual(response.status_code,200)
    self.assertEqual(response.json()['status'],'ready')
    self.assertEqual(response.json()['configuration_version'],initial.version)
 def test_failed_configuration_recheck_cannot_return_ready_or_leak_error(self):
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'settings.db';store=SettingsStore(path)
   initial=store.save('station-1',parameters(),expected_revision=0)
   class Service:
    def fetch(self,configuration):return {'station_id':configuration.station_id,'status':'ready','issues':[]}
   with patch('m4_settings.api.SettingsStore.get',side_effect=[initial,OSError('sensitive disk error')]):
    with TestClient(create_app(path,input_service=Service())) as client:
     response=client.get('/m4-api/stations/station-1/inputs')
     self.assertEqual(response.status_code,503)
     self.assertNotIn('sensitive',response.text)

if __name__=='__main__':unittest.main()
