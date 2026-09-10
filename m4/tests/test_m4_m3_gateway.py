import json
import unittest
from unittest.mock import Mock, patch

from m4.settings.m3_current_result import M3CurrentResult


class GatewayTests(unittest.TestCase):
    def test_explicit_gateway_uses_fixed_readonly_route_and_unwraps_data(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps({'status': 'ok', 'data': {'run_id': 'test'}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.dict('os.environ', {'M4_M3_TRANSPORT': 'platform_gateway'}), patch(
                'm4.settings.m3_current_result.build_opener', return_value=opener):
            result = M3CurrentResult(gateway_token='test-platform-token').get(
                '/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1')
        self.assertEqual(result, {'run_id': 'test'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://opdash.lvkpower.com/energy-forecast-api/custom-runs/station_2/latest?interval_seconds=900&forecast_days=1')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-platform-token')

    def test_unknown_path_and_missing_credential_rejected_before_network(self):
        with patch.dict('os.environ', {'M4_M3_TRANSPORT': 'platform_gateway'}), patch(
                'm4.settings.m3_current_result.build_opener') as network:
            with self.assertRaises(ValueError):
                M3CurrentResult(gateway_token='test-platform-token').get('https://other.invalid/')
            with self.assertRaises(ValueError):
                M3CurrentResult().get('/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1')
            network.assert_not_called()

    def test_platform_error_is_not_accepted_as_result(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"error","data":{}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.dict('os.environ', {'M4_M3_TRANSPORT': 'platform_gateway'}), patch(
                'm4.settings.m3_current_result.build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                M3CurrentResult(gateway_token='test-platform-token').get(
                    '/v1/custom-forecast-runs/f98ae7a6-c749-4ddf-b1c0-e90488136a20/result')

    def test_upstream_passes_existing_platform_token(self):
        from m4.settings.upstream import NocoBaseClient
        with patch('m4.settings.m3_current_result.M3CurrentResult') as factory:
            NocoBaseClient('test-platform-token').current_load_result('ES02')
            factory.assert_called_once_with(gateway_token='test-platform-token')
            factory.return_value.read.assert_called_once_with('ES02')
