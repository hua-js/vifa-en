"""Acceptance summary responses may include NocoBase audit metadata."""
import unittest
from m3.worker.errors import M3Error
from m3.worker.services.acceptance_run_service import AcceptanceRunService, _run_record


def record():
    return dict(id=1, station_id='ES01', acceptance_run_id='acceptance-20260829-station1',
                window_start='2026-08-28T17:00:00.000Z', window_end='2026-09-04T17:00:00.000Z',
                control_state='active', completed_days=0, result_state='pending', calculated_at=None)


class SummaryApi:
    def __init__(self, changes=None):
        self.changes = changes or {}

    def list_records(self, *args, **kwargs):
        return [record()]

    def update_record(self, collection, record_id, values):
        return {**record(), **values, 'createdAt': '2026-08-28T15:10:31.824Z',
                'updatedAt': '2026-09-19T01:00:00+08:00', 'createdById': 1,
                'updatedById': 1, **self.changes}


class AcceptanceSummaryResponseTests(unittest.TestCase):
    def update(self, changes=None):
        return AcceptanceRunService(SummaryApi(changes))._update_summary(
            _run_record(record()), completed_days=0, result_state='pending', calculated_at=None)

    def test_summary_update_accepts_system_audit_fields(self):
        self.assertEqual(self.update().completed_days, 0)

    def test_summary_update_still_rejects_changed_identity_or_value(self):
        for changes in ({'id': 2}, {'station_id': 'ES02'}, {'completed_days': 1}):
            with self.subTest(changes=changes), self.assertRaises(M3Error) as caught:
                self.update(changes)
            self.assertEqual(caught.exception.code, 'acceptance_run_update_incomplete')

    def test_missing_business_field_and_unknown_extra_field_are_rejected(self):
        missing = record()
        missing.pop('calculated_at')
        for value in (missing, {**record(), 'unexpected': True}):
            with self.assertRaises(M3Error):
                _run_record(value)
