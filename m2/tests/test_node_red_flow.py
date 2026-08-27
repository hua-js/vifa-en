import json
from pathlib import Path
import subprocess
import unittest


FLOW_PATH = Path(__file__).resolve().parents[1] / "node-red-energy-efficiency-api-flow.json"
NODE_RUNNER = """
const source = process.argv[1];
const message = JSON.parse(process.argv[2]);
const result = Function('msg', source)(message);
process.stdout.write(JSON.stringify(result));
"""


class NodeRedFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
        cls.by_name = {node.get("name"): node for node in cls.flow}
        cls.by_id = {node["id"]: node for node in cls.flow}

    def _run_function(self, node_name, message):
        completed = subprocess.run(
            [
                "node",
                "-e",
                NODE_RUNNER,
                self.by_name[node_name]["func"],
                json.dumps(message),
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    def test_calendar_minute_job_runs_each_station_independently_with_fixed_commands(self):
        accepted_station = self._run_function(
            "检查场站",
            {"req": {"query": {"station_id": "es01"}}},
        )
        self.assertEqual(accepted_station[0]["payload"], "ES01")
        self.assertIsNone(accepted_station[1])

        missing_station = self._run_function(
            "检查场站",
            {"req": {"query": {}}},
        )
        self.assertIsNone(missing_station[0])
        self.assertEqual(missing_station[1]["statusCode"], 400)
        self.assertEqual(
            missing_station[1]["payload"]["error"]["message"],
            "station_id 只能是 ES01 或 ES02",
        )
        invalid_station = self._run_function(
            "检查场站",
            {"req": {"query": {"station_id": "ES03"}}},
        )
        self.assertIsNone(invalid_station[0])
        self.assertEqual(invalid_station[1]["statusCode"], 400)

        commands = self._run_function(
            "构造双电站分钟命令",
            {"payload": 0, "topic": "ignored"},
        )
        self.assertEqual(
            [message["payload"] for message in commands[0]],
            [
                "python3 /userdata/holo/pyfiles/energy-efficiency-api.py minute ES01",
                "python3 /userdata/holo/pyfiles/energy-efficiency-api.py minute ES02",
            ],
        )
        self.assertEqual(commands[0][0]["topic"], "ignored")
        self.assertEqual(commands[0][1]["topic"], "ignored")

        schedule = self.by_name["每分钟触发"]
        self.assertEqual(schedule["repeat"], "")
        self.assertEqual(schedule["crontab"], "* * * * *")
        self.assertEqual(schedule["onceDelay"], "5")
        self.assertFalse(schedule["once"])
        self.assertTrue(schedule["d"])

    def test_dashboard_only_returns_parsed_single_line_success_json(self):
        accepted = self._run_function(
            "包装JSON结果",
            {"payload": '{"status":"ok","data":{"station_id":"ES01"}}'},
        )
        self.assertEqual(accepted["statusCode"], 200)
        self.assertEqual(accepted["payload"]["data"]["station_id"], "ES01")

        for raw_output in (
            '{"status":"ok"}\n{"status":"ok"}',
            '{"status":"error","error":{"message":"untrusted-secret"}}',
        ):
            with self.subTest(raw_output=raw_output):
                rejected = self._run_function("包装JSON结果", {"payload": raw_output})
                self.assertEqual(rejected["statusCode"], 500)
                self.assertEqual(
                    rejected["payload"]["error"]["message"],
                    "Python 没有返回正确的 JSON",
                )
                self.assertNotIn("untrusted-secret", json.dumps(rejected))

        dashboard_exec = self.by_name["执行能效计算脚本"]
        self.assertEqual(dashboard_exec["addpay"], "payload")
        self.assertEqual(dashboard_exec["useSpawn"], "false")
        self.assertEqual(dashboard_exec["append"], " 2>&1")

        self.assertEqual(
            self._run_function("处理看板退出状态", {"payload": {"code": 0}}),
            [None, None],
        )
        dashboard_failed = self._run_function(
            "处理看板退出状态",
            {"payload": {"code": 1}},
        )
        self.assertEqual(dashboard_failed[0]["statusCode"], 502)
        self.assertEqual(
            dashboard_failed[0]["payload"]["error"]["message"],
            "Python 执行失败",
        )
        self.assertNotIn("code", json.dumps(dashboard_failed[0]["payload"]))
        self.assertIn("服务器受控日志", dashboard_failed[1]["payload"]["message"])
        self.assertEqual(
            self._run_function("丢弃看板stderr", {"payload": "untrusted-secret"}),
            None,
        )

    def test_daily_cleanup_is_fixed_command_and_exec_outputs_are_redacted(self):
        cleanup_inject = self.by_name["每日清理30天前设备点"]
        cleanup_exec = self.by_name["执行设备点清理"]
        self.assertEqual(cleanup_inject["crontab"], "10 2 * * *")
        self.assertEqual(cleanup_inject["repeat"], "")
        self.assertFalse(cleanup_inject["once"])
        self.assertTrue(cleanup_inject["d"])
        self.assertEqual(
            cleanup_exec["command"],
            "python3 /userdata/holo/pyfiles/energy-efficiency-api.py cleanup",
        )
        self.assertFalse(cleanup_exec["addpay"])
        self.assertEqual(cleanup_exec["useSpawn"], "false")

        minute_exec = self.by_name["写入双电站分钟效率"]
        self.assertTrue(minute_exec["addpay"])
        self.assertEqual(minute_exec["useSpawn"], "false")

        group = self.by_name["能效分析API"]
        self.assertTrue({
            cleanup_inject["id"],
            cleanup_exec["id"],
            self.by_name["脱敏清理结果"]["id"],
            self.by_name["设备点清理结果"]["id"],
            self.by_name["设备点清理错误（脱敏）"]["id"],
        }.issubset(set(group["nodes"])))

        flow_text = json.dumps(self.flow).lower()
        self.assertNotIn("token", flow_text)
        self.assertNotIn("secret", flow_text)

        for exec_name in (
            "执行能效计算脚本",
            "写入双电站分钟效率",
            "执行设备点清理",
        ):
            exec_node = self.by_name[exec_name]
            for error_wire in exec_node["wires"][1:]:
                for target_id in error_wire:
                    self.assertNotEqual(
                        self.by_id[target_id]["type"],
                        "debug",
                        f"{exec_name} must redact stderr/exit output before debug",
                    )

    def test_desensitized_minute_and_cleanup_handlers_cover_json_stderr_and_exit_code(self):
        minute_ok = self._run_function(
            "脱敏分钟写入结果",
            {"payload": '{"status":"partial","data":{"operation":"minute","station_id":"ES02","data_time":"2026-08-27T10:00:00+08:00","device_points_saved":2,"event_update_count":1,"warning_count":1}}'},
        )
        self.assertEqual(minute_ok[0]["payload"]["station_id"], "ES02")
        self.assertIsNone(minute_ok[1])

        minute_non_json = self._run_function(
            "脱敏分钟写入结果",
            {"payload": "untrusted-secret"},
        )
        self.assertIsNone(minute_non_json[0])
        self.assertNotIn("untrusted-secret", json.dumps(minute_non_json[1]))
        self.assertEqual(
            self._run_function("丢弃分钟stderr", {"payload": "untrusted-secret"}),
            None,
        )
        self.assertEqual(
            self._run_function("处理分钟退出状态", {"payload": {"code": 0}}),
            None,
        )
        minute_nonzero = self._run_function(
            "处理分钟退出状态",
            {"payload": {"code": 1}},
        )
        self.assertNotIn("code", json.dumps(minute_nonzero))
        self.assertIn("服务器受控日志", minute_nonzero["payload"]["message"])

        cleanup_ok = self._run_function(
            "脱敏清理结果",
            {"payload": '{"status":"ok","data":{"operation":"cleanup","cutoff":"2026-07-28T10:00:00+08:00","deleted_count":2}}'},
        )
        self.assertEqual(cleanup_ok[0]["payload"]["deleted_count"], 2)
        self.assertIsNone(cleanup_ok[1])
        cleanup_non_json = self._run_function(
            "脱敏清理结果",
            {"payload": "untrusted-secret"},
        )
        self.assertIsNone(cleanup_non_json[0])
        self.assertNotIn("untrusted-secret", json.dumps(cleanup_non_json[1]))
        self.assertEqual(
            self._run_function("丢弃清理stderr", {"payload": "untrusted-secret"}),
            None,
        )
        self.assertEqual(
            self._run_function("处理清理退出状态", {"payload": {"code": 0}}),
            None,
        )
        cleanup_nonzero = self._run_function(
            "处理清理退出状态",
            {"payload": {"code": 1}},
        )
        self.assertNotIn("code", json.dumps(cleanup_nonzero))
        self.assertIn("服务器受控日志", cleanup_nonzero["payload"]["message"])


if __name__ == "__main__":
    unittest.main()
