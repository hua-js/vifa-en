import unittest
from m1.deploy import node_red_gateway as gateway


class GatewayTests(unittest.TestCase):
    def test_only_three_m1_nodes_change_and_old_exec_is_replaced(self):
        nodes = [
            {"id": gateway.INPUT_ID, "type": "http in", "url": "/dashboard_energy_api"},
            {"id": gateway.PREPARE_ID, "type": "function", "func": "old", "wires": [[gateway.REQUEST_ID]]},
            {"id": gateway.REQUEST_ID, "type": "exec", "command": "old launcher", "x": 10, "y": 20, "z": "tab", "wires": [[gateway.WRAP_ID], [], []]},
            {"id": gateway.WRAP_ID, "type": "function", "func": "old", "wires": [[gateway.RESPONSE_ID]]},
            {"id": gateway.RESPONSE_ID, "type": "http response", "wires": []},
            {"id": "other", "type": "template", "template": "unchanged other module"},
        ]
        updated = gateway.update_gateway(nodes)
        changed = {before["id"] for before, after in zip(nodes, updated) if before != after}
        self.assertEqual(changed, {gateway.PREPARE_ID, gateway.REQUEST_ID, gateway.WRAP_ID})
        request = next(node for node in updated if node["id"] == gateway.REQUEST_ID)
        self.assertEqual(request["type"], "http request")
        self.assertEqual(request["url"], "http://127.0.0.1:8811/dashboard_energy_api")
        self.assertEqual(request["wires"], [[gateway.WRAP_ID]])
        self.assertFalse(request["followRedirects"])
        self.assertNotIn("command", request)
        self.assertEqual(nodes[-1], updated[-1])

    def test_unknown_flow_fails_without_modifying_input(self):
        nodes = [{"id": "other", "type": "template", "template": "unchanged"}]
        with self.assertRaises(ValueError):
            gateway.update_gateway(nodes)
        self.assertEqual(nodes[0]["template"], "unchanged")
