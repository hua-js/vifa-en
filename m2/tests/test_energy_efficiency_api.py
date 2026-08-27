import importlib.util
from pathlib import Path
import unittest


ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "energy-efficiency-api.py"
SPEC = importlib.util.spec_from_file_location("energy_efficiency_api", ENTRYPOINT_PATH)
energy_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(energy_api)

ENVIRONMENT = {
    "VIFA_EMU_URL": "https://station.example/api/t_emu:list?filter=%7B%7D",
    "VIFA_EMU_TOKEN": "source-secret",
    "M2_NOCOBASE_BASE_URL": "https://station.example",
    "M2_NOCOBASE_TOKEN": "store-secret",
    "M2_TIMEZONE": "Asia/Shanghai",
    "M2_REQUEST_TIMEOUT_SECONDS": "10",
}


class EnergyEfficiencyApiTests(unittest.TestCase):
    def test_minute_command_saves_one_station_and_returns_small_json_result(self):
        def process_minute(station_id, config):
            return {
                "minute_point": {
                    "station_id": station_id,
                    "data_time": "2026-08-26T09:30:00+08:00",
                },
                "saved_record": {"id": 81},
            }

        payload, exit_code = energy_api.execute(
            ["minute", "ES01"],
            ENVIRONMENT,
            process_minute=process_minute,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload,
            {
                "status": "ok",
                "data": {
                    "operation": "minute",
                    "station_id": "ES01",
                    "data_time": "2026-08-26T09:30:00+08:00",
                    "saved_id": 81,
                },
            },
        )

    def test_dashboard_reads_today_points_and_returns_beijing_time_curve(self):
        point = {
            "station_id": "ES02",
            "data_time": "2026-08-26T09:30:00+08:00",
            "pv_storage_efficiency": 88.5,
            "storage_load_efficiency": None,
            "pv_load_efficiency": 100.0,
            "pv_storage_input_kw": 100.0,
            "pv_storage_output_kw": 88.5,
            "storage_load_input_kw": None,
            "storage_load_output_kw": None,
            "pv_load_input_kw": 80.0,
            "pv_load_output_kw": 80.0,
            "formula_version": "energy-chain-v1",
            "calculated_at": "2026-08-26T09:30:03+08:00",
        }

        def fetch_station(station_id, config):
            return {
                "bus_id": station_id,
                "data_time": "2026-08-26T01:30:27Z",
                "source_times": {
                    "emu:emu21": "2026-08-26T01:30:26Z",
                },
            }

        def calculate(source):
            return {
                "bus_id": source["bus_id"],
                "data_time": source["data_time"],
                "calculated_at": "2026-08-26T01:30:29Z",
                "calculation_mode": "estimated",
            }

        def fetch_points(station_id, start_time, end_time, config):
            if (
                station_id,
                start_time,
                end_time,
            ) == (
                "ES02",
                "2026-08-26T00:00:00+08:00",
                "2026-08-27T00:00:00+08:00",
            ):
                return [point]
            return []

        payload, exit_code = energy_api.execute(
            ["dashboard", "ES02"],
            ENVIRONMENT,
            fetch_station=fetch_station,
            calculate=calculate,
            fetch_points=fetch_points,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["range"]["timezone"], "Asia/Shanghai")
        self.assertEqual(
            payload["data"]["realtime"]["inputs"]["data_time"],
            "2026-08-26T09:30:27+08:00",
        )
        self.assertEqual(
            payload["data"]["realtime"]["inputs"]["source_times"],
            {"emu:emu21": "2026-08-26T09:30:26+08:00"},
        )
        self.assertEqual(
            payload["data"]["realtime"]["result"]["calculated_at"],
            "2026-08-26T09:30:29+08:00",
        )
        self.assertEqual(
            payload["data"]["trend"],
            [{
                "data_time": "2026-08-26T09:30:00+08:00",
                "pvStorage": 88.5,
                "storageLoad": None,
                "pvLoad": 100.0,
            }],
        )


if __name__ == "__main__":
    unittest.main()
