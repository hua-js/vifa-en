"""Read local snapshots and emit a selection preview to stdout."""
import argparse
import json
from pathlib import Path
import sys

from m4_optimizer.contracts import OptimizationRequest, OptimizationResult
from m4_selection import SelectionPolicy, select_candidate


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f'nonfinite JSON constant: {value}')


def _load(path, contract):
    # JSON mode accepts ISO datetimes while preserving the strict contracts.
    data = json.loads(Path(path).read_text(encoding='utf-8'),
                      object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    return contract.model_validate_json(json.dumps(data, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='M4 本地候选选择预览，不下发设备指令')
    parser.add_argument('--request', required=True, help='OptimizationRequest JSON')
    parser.add_argument('--result', required=True, help='OptimizationResult JSON (非 B1 外层结果)')
    parser.add_argument('--policy', help='显式 SelectionPolicy JSON；省略则待配置')
    args = parser.parse_args(argv)
    try:
        request = _load(args.request, OptimizationRequest)
        result = _load(args.result, OptimizationResult)
        policy = _load(args.policy, SelectionPolicy) if args.policy is not None else None
        preview = select_candidate(request, result, policy)
    except (OSError, ValueError, OverflowError) as exc:
        print(f'选择失败：{exc}', file=sys.stderr)
        return 2
    print(preview.model_dump_json(indent=2))
    return 0 if preview.status == 'selected' else 1


if __name__ == '__main__':
    raise SystemExit(main())
