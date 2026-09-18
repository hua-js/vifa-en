import json
from pathlib import Path
import subprocess
import unittest


FLOW_PATH = Path(__file__).resolve().parents[1] / "node_red" / "node-red-energy-efficiency-api-flow.json"
NODE_RUNNER = """
const source = process.argv[1];
const message = JSON.parse(process.argv[2]);
const result = Function('msg', source)(message);
process.stdout.write(JSON.stringify(result));
"""


class NodeRedFlowTests(unittest.TestCase):
    def test_store_diagnostic_redacts_untrusted_stderr_fields(self):
        diagnostic = {
            "source": "m2_store_diagnostic",
            "collection": "t_efficiency_bottleneck_events",
            "operation": "updateOrCreate",
            "error_type": "http_error",
            "http_status": 403,
            "elapsed_ms": 123,
            "message": "PRIVATE",
            "token": "PRIVATE",
        }
        result = self._run_function("解析分钟存储诊断", {
            "m2_station_id": "ES02",
            "payload": 'PRIVATE\n' + json.dumps(diagnostic),
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["payload"]["station_id"], "ES02")
        self.assertEqual(result["payload"]["diagnostics"][0]["http_status"], 403)
        self.assertNotIn("PRIVATE", json.dumps(result))
        diagnostic["collection"] = "PRIVATE"
        self.assertIsNone(self._run_function("解析分钟存储诊断", {"payload": json.dumps(diagnostic)}))

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
        self.assertEqual(accepted_station[0]["payload"], "dashboard ES01")
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
        self.assertEqual(
            [message["m2_station_id"] for message in commands[0]],
            ["ES01", "ES02"],
        )
        self.assertEqual(commands[0][0]["topic"], "ignored")
        self.assertEqual(commands[0][1]["topic"], "ignored")

        schedule = self.by_name["每分钟触发"]
        self.assertEqual(schedule["repeat"], "")
        self.assertEqual(schedule["crontab"], "* * * * *")
        self.assertEqual(schedule["onceDelay"], "5")
        self.assertFalse(schedule["once"])
        self.assertTrue(schedule["d"])

    def test_history_queries_validate_dates_and_allow_only_read_operations(self):
        for query, expected in (
            ({"operation": "history", "date": "2026-08-25"}, "history ES02 2026-08-25"),
            ({"operation": "events", "start_date": "2026-08-01", "end_date": "2026-08-31"}, "events ES02 2026-08-01 2026-08-31"),
        ):
            accepted = self._run_function("检查场站", {"req": {"query": {"station_id": "ES02", **query}}})
            self.assertEqual(accepted[0]["payload"], expected)
            self.assertIsNone(accepted[1])
        invalid = [
            {"operation": "minute"}, {"operation": "cleanup"}, {"operation": ["history"]},
            {"operation": "history", "date": "2026-02-30"},
            {"operation": "history", "date": "2026-08-25;id"},
            {"operation": "history", "date": ["2026-08-25"]},
            {"operation": "history", "date": "9999-12-31"},
            {"operation": "events", "start_date": "2026-08-01", "end_date": "2026-09-01"},
            {"operation": "events", "start_date": "2026-08-25", "end_date": "2026-08-24"},
            {"operation": "events", "start_date": "2026-08-25"},
        ]
        for query in invalid:
            with self.subTest(query=query):
                rejected = self._run_function("检查场站", {"req": {"query": {"station_id": "ES02", **query}}})
                self.assertIsNone(rejected[0])
                self.assertEqual(rejected[1]["statusCode"], 400)
        self.assertEqual(self.by_name["执行能效计算脚本"]["command"],
                         "python3 /userdata/holo/pyfiles/energy-efficiency-api.py")

    def test_dashboard_only_returns_parsed_single_line_success_json(self):
        accepted = self._run_function(
            "包装JSON结果",
            {"payload": '{"status":"ok","data":{"station_id":"ES01"}}'},
        )
        self.assertEqual(accepted["statusCode"], 200)
        self.assertEqual(accepted["payload"]["data"]["station_id"], "ES01")

        for raw_output in (
            "",
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
            None,
        )
        dashboard_failed = self._run_function(
            "处理看板退出状态",
            {"payload": {"code": 1}},
        )
        self.assertNotIn("code", json.dumps(dashboard_failed))
        self.assertIn("服务器受控日志", dashboard_failed["payload"]["message"])
        rc_handler = self.by_name["处理看板退出状态"]
        http_response_id = self.by_name["返回能效数据"]["id"]
        self.assertNotIn(http_response_id, [
            target_id
            for wire in rc_handler["wires"]
            for target_id in wire
        ])
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
            {"payload": '{"status":"partial","data":{"operation":"minute","station_id":"ES02","data_time":"2026-08-27T10:00:00+08:00","device_points_saved":2,"event_update_count":1,"event_persistence":{"attempted":3,"saved":2,"failed":1,"outbox_pending":1},"warning_count":1}}'},
        )
        self.assertEqual(minute_ok[0]["payload"]["station_id"], "ES02")
        self.assertEqual(minute_ok[0]["payload"]["event_persistence"], {
            "attempted": 3, "saved": 2, "failed": 1, "outbox_pending": 1,
        })
        self.assertIsNone(minute_ok[1])

        minute_non_json = self._run_function(
            "脱敏分钟写入结果",
            {"payload": "untrusted-secret"},
        )
        self.assertIsNone(minute_non_json[0])
        self.assertNotIn("untrusted-secret", json.dumps(minute_non_json[1]))
        self.assertEqual(
            self._run_function("解析分钟存储诊断", {"payload": "untrusted-secret"}),
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
        self.assertEqual(minute_nonzero["payload"]["error_code"], "internal_error")
        self.assertEqual(minute_nonzero["payload"]["message"], "服务器内部错误")

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

    def test_minute_stdout_classifies_only_allowlisted_public_errors(self):
        classified = self._run_function(
            "脱敏分钟写入结果",
            {
                "m2_station_id": "ES02",
                "payload": json.dumps({
                    "status": "error",
                    "error": {
                        "code": "store_error",
                        "message": "分钟效率数据读写失败",
                    },
                }),
            },
        )
        self.assertIsNone(classified[0])
        self.assertEqual(classified[1]["payload"], {
            "status": "error",
            "source": "minute_job",
            "station_id": "ES02",
            "error_code": "store_error",
            "message": "分钟效率数据读写失败",
        })

        untrusted = self._run_function(
            "脱敏分钟写入结果",
            {
                "m2_station_id": "ES01",
                "payload": json.dumps({
                    "status": "error",
                    "error": {
                        "code": "attacker_defined",
                        "message": "token=untrusted-secret",
                    },
                }),
            },
        )
        self.assertIsNone(untrusted[0])
        self.assertEqual(untrusted[1]["payload"], {
            "status": "error",
            "source": "minute_job",
            "station_id": "ES01",
            "error_code": "invalid_output",
            "message": "分钟任务没有返回可用结果",
        })
        self.assertNotIn("untrusted-secret", json.dumps(untrusted))

        malicious_success = self._run_function(
            "脱敏分钟写入结果",
            {
                "m2_station_id": "ES01",
                "payload": json.dumps({
                    "status": "ok",
                    "data": {
                        "operation": "minute",
                        "station_id": "ES01",
                        "data_time": "https://untrusted.example/?token=untrusted-secret",
                        "device_points_saved": 1,
                        "event_update_count": 0,
                        "event_persistence": {
                            "attempted": 0,
                            "saved": 0,
                            "failed": 0,
                            "outbox_pending": 0,
                        },
                        "warning_count": 0,
                    },
                }),
            },
        )
        self.assertIsNone(malicious_success[0])
        self.assertEqual(
            malicious_success[1]["payload"]["error_code"],
            "invalid_output",
        )
        self.assertNotIn("untrusted-secret", json.dumps(malicious_success))
        self.assertNotIn("untrusted.example", json.dumps(malicious_success))

    def test_minute_exit_status_classifies_nonzero_and_interrupted_processes(self):
        store_failure = self._run_function(
            "处理分钟退出状态",
            {"m2_station_id": "ES02", "payload": {"code": 6}},
        )
        self.assertEqual(store_failure["payload"], {
            "status": "error",
            "source": "minute_job",
            "station_id": "ES02",
            "error_code": "store_error",
            "message": "分钟效率数据读写失败",
        })

        interrupted = self._run_function(
            "处理分钟退出状态",
            {
                "m2_station_id": "ES01",
                "payload": {"code": None, "signal": "untrusted-signal"},
            },
        )
        self.assertEqual(interrupted["payload"], {
            "status": "error",
            "source": "minute_job",
            "station_id": "ES01",
            "error_code": "process_interrupted",
            "message": "分钟任务被超时或信号终止",
        })
        self.assertNotIn("untrusted-signal", json.dumps(interrupted))


if __name__ == "__main__":
    unittest.main()
