#!/usr/bin/env python3
"""Validate a project file offline; never read secrets, start jobs or call APIs."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, help='Project JSON; defaults to VIFA_PROJECT_CONFIG or built-in VIFA configuration')
    args = parser.parse_args()
    if args.project is not None:
        os.environ['VIFA_PROJECT_CONFIG'] = str(args.project.resolve())
    try:
        from shared.project import get_project
        metadata = get_project().public_metadata()
    except (OSError, ValueError, TypeError, KeyError):
        # Configuration errors must not leak private URLs or accidental credentials.
        print('项目配置校验失败，请检查版本、必填项、站点/设备归属及数据源格式。', file=sys.stderr)
        return 1
    print(json.dumps({'valid': True, **metadata}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
