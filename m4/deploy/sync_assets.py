#!/usr/bin/env python3
"""Generate M4 checked-in delivery assets from canonical HTML and Function sources."""
import argparse
import json
from pathlib import Path

from build_package import DEPLOY, HTML, ROOT, flow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Update generated repository assets; otherwise only check')
    args = parser.parse_args()
    html = HTML.read_text(encoding='utf-8')
    assets = {
        ROOT / 'm4/node_red/m4_customer_template.html': html,
        ROOT / 'm4/node_red/m4_customer_flow.json': json.dumps(flow(html), ensure_ascii=False, indent=2) + '\n',
        ROOT / 'm4/node_red/m4_prepare_proxy.js': (DEPLOY / 'node_red/prepare_proxy.js').read_text(encoding='utf-8'),
    }
    stale = []
    for path, content in assets.items():
        if not path.is_file() or path.read_text(encoding='utf-8') != content:
            stale.append(str(path.relative_to(ROOT)))
            if args.write:
                path.write_text(content, encoding='utf-8')
    print(json.dumps({'mode': 'write' if args.write else 'check', 'changed' if args.write else 'stale': stale}, ensure_ascii=False))
    return 0 if args.write or not stale else 1


if __name__ == '__main__':
    raise SystemExit(main())
