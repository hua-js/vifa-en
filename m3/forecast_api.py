#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3.worker.dashboard_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
