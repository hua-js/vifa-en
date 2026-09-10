import importlib.metadata
import unittest

import m3.worker


class M3EnvironmentTests(unittest.TestCase):
    def test_locked_runtime_versions(self):
        self.assertEqual(m3.worker.__version__, "0.1.0")
        self.assertEqual(importlib.metadata.version("statsforecast"), "2.1.1")
        self.assertEqual(importlib.metadata.version("fastapi"), "0.141.1")
        self.assertEqual(importlib.metadata.version("httpx"), "0.28.1")
