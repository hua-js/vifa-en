"""Run existing offline unit tests by module from any working directory."""
import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("m1", "m2", "m3", "m4")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="*", metavar="MODULE", help="m1 m2 m3 m4; default: all")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    modules = args.modules or list(MODULES)
    if any(module not in MODULES for module in modules):
        parser.error("modules must be chosen from m1, m2, m3, m4")
    paths = [str(ROOT), *(str(ROOT / module / "tests") for module in MODULES)]
    sys.path[:0] = paths
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
    os.environ.setdefault("M4_NOCOBASE_TOKEN", "test-token")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    suite = unittest.TestSuite()
    for module in dict.fromkeys(modules):
        suite.addTests(unittest.TestLoader().discover(str(ROOT / module / "tests"), top_level_dir=str(ROOT)))
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
