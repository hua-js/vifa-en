from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4
from fastapi.testclient import TestClient
from m4.settings.api import create_app
from m4.settings.allocation_records import AllocationError


class AllocationApiTests(TestCase):
    def test_explicit_save_and_read_only_gets(self):
        calls=[]
        class Service:
            def create(self,*a):calls.append(('save',a));return {'allocation_id':a[1],'station_id':a[0],'dispatch_status':'not_dispatched'}
            def history(self,*a,**kw):calls.append(('history',kw));return {'items':[]}
            def get(self,*a):calls.append(('get',a));return {'allocation_id':a[1]}
        with TemporaryDirectory() as tmp,TestClient(create_app(Path(tmp)/'settings.db',allocation_service=Service())) as c:
            prefix='/m4-api/stations/station-1/'
            key,run=str(uuid4()),str(uuid4())
            self.assertEqual(c.get(prefix+'allocations').status_code,200)
            self.assertFalse(any(a[0]=='save' for a in calls))
            response=c.post(prefix+'allocations',json={'request_id':key,'plan_run_id':run})
            self.assertEqual(response.status_code,201)
            self.assertEqual(response.json()['dispatch_status'],'not_dispatched')
            self.assertEqual(c.get(prefix+'allocation-result',params={'allocation_id':key}).status_code,200)
            self.assertEqual(c.get(prefix+'allocations',params={'run_id':run,'limit':21}).status_code,422)
            self.assertEqual(c.post(prefix+'allocations',json={'request_id':key,'plan_run_id':run,'power_kw':999}).status_code,422)
            self.assertEqual(c.post(prefix+'allocations',headers={'Origin':'https://other.example'},json={'request_id':key,'plan_run_id':run}).status_code,403)
            self.assertEqual(c.get('/m4-api/stations/other/allocations').status_code,404)

    def test_expected_conflict_and_unexpected_error_sanitized(self):
        class Service:
            def create(self,*a):raise AllocationError(409,'当前计划已变化')
            def history(self,*a,**kw):raise RuntimeError('secret-diagnostic')
        with TemporaryDirectory() as tmp,TestClient(create_app(Path(tmp)/'settings.db',allocation_service=Service())) as c:
            url='/m4-api/stations/station-1/allocations'
            self.assertEqual(c.post(url,json={'request_id':str(uuid4()),'plan_run_id':str(uuid4())}).status_code,409)
            response=c.get(url);self.assertEqual(response.status_code,503);self.assertNotIn('secret-diagnostic',response.text)
