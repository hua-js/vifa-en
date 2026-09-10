#!/usr/bin/env python3
"""Produce an immutable local ES02 retrospective-weather backtest bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from m3_worker.domain import pv_training, pv_backtest

CSV_FIELDS = dict(zip(
    ('气温(℃)', '相对湿度(%)', '总云量(%)', '低层云量(%)', '中层云量(%)', '高层云量(%)',
     '短波辐射(W/㎡)', '直接辐射(W/㎡)', '散射辐射(W/㎡)', '降水量(mm)', '10米风速(km/h)'),
    ('temperature_c', 'relative_humidity_pct', 'cloud_cover_pct', 'cloud_cover_low_pct',
     'cloud_cover_mid_pct', 'cloud_cover_high_pct', 'ghi_wm2', 'direct_horizontal_wm2',
     'dhi_wm2', 'precipitation_mm', 'wind_speed_kmh')))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def report_markdown(metrics, quality, folds, manifest):
    ridge = metrics['models']['WeatherRidge']['irradiated']
    baseline = metrics['models']['SeasonalNaive']['irradiated']
    improvement = (1-ridge['mae_kw']/baseline['mae_kw'])*100 if baseline['mae_kw'] else None
    lines = [
        '# 电站2光伏功率：第一轮天气回测', '',
        '**已完成离线样本与回测；尚未发布运行预测或接入调度。**', '',
        f"在{metrics['irradiated_points']}个有辐照的共同时间点上，WeatherRidge的MAE为{ridge['mae_kw']:.2f} kW，"
        f"昨日同刻为{baseline['mae_kw']:.2f} kW。"
        + (f'相对基线降低{improvement:.1f}%。' if improvement is not None else ''), '',
        '辐照比例模型的MAE略低，完整日电量误差也较低；Ridge的RMSE较低。本轮没有证明Ridge全面优于更简单的天气模型。'
        '三种方法事先固定，未用这些测试日筛选正则参数或追加特征。', '',
        '**这些天气模型使用事后已知的再分析天气，结果不等于真实天气预报条件下的预测精度。**', '',
        '## 数据与覆盖', '',
        f"- 电站ES02，来源坐标23/113，Asia/Shanghai；实测快照共{quality['pv_raw_rows']:,}条。",
        f"- 完整保留已入库天气的相同CSV来源文件，共{quality['weather_rows']:,}行；本轮校验文件SHA与入库报告一致，没有重新查询远程数据库。",
        f"- 合格样本池{quality['eligible_quarters']:,}个十五分钟点（{quality['eligible_hours']}小时），{quality['first_eligible']}至{quality['last_eligible_end']}，右端为区间结束。",
        f"- 首折训练{folds[0]['train_rows']}点，包含{folds[0]['complete_training_days']}个完整日；之后逐日扩大训练窗口。",
        f"- 回测{len(folds)}天，{metrics['target_grid_points']}个预期点；天气和实测合格{metrics['eligible_points']}点，三模型共同评分{metrics['shared_points']}点。",
        f"- 有辐照时段定义GHI>20 W/m²，共{metrics['irradiated_points']}个共同点；完整全天能量可比较{metrics['shared_full_days']}天。", '',
        '## 模型与功率误差', '',
        '| 模型 | 全部共同点 MAE/kW | 有辐照 MAE/kW | 有辐照 RMSE/kW | 有辐照 WAPE/% | 完整日电量 MAE/kWh |',
        '|---|---:|---:|---:|---:|---:|']
    for name, value in metrics['models'].items():
        day = value['irradiated']
        energy = value['daily_energy_mae_kwh']
        energy_text = f'{energy:.2f}' if energy is not None else '—'
        lines.append(f"| {name} | {value['all']['mae_kw']:.2f} | {day['mae_kw']:.2f} | {day['rmse_kw']:.2f} | "
                     f"{day['wape_pct']:.2f} | {energy_text} |")
    lines += ['', 'WAPE=绝对误差总和/实测功率总和，不表述为“准确率”；不使用夜间零功率的逐点MAPE。', '',
              '## 日发电量与缺口', '',
              '| 日期 | 共同点/96 | 实测估计/kWh | 昨日同刻/kWh | 辐照比例/kWh | Ridge/kWh |',
              '|---|---:|---:|---:|---:|---:|']
    for day in metrics['daily']:
        if day['full_day_comparable']:
            energies = [f"{day['models'][name]['predicted_kwh']:.1f}" for name in pv_backtest.MODEL_COLUMNS]
            lines.append(f"| {day['date']} | 96 | {day['actual_kwh']:.1f} | " + ' | '.join(energies) + ' |')
        else:
            lines.append(f"| {day['date']} | {day['shared_points']} | — | — | — | — |")
    lines += ['',
        '8月27日有一个功率区间不足覆盖门槛，整小时4点排除；8月28日昨日基线在对应区间缺1点；'
        '8月31日23–24点需要9月1日00:00的小时结束天气，原CSV不包含。以上日期不作为三模型共同的全天能量比较。', '',
        '## 时间语义与模型实现', '',
        '- 实测先按分钟求均值，避免同分钟多次采样增加权重，再求十五分钟均值；每点至少12个有效分钟，四点合格才纳入该小时。缺值不补零。',
        '- 辐照取小时结束标签，整小时内四点共用小时均值；降水均分四份。温度/湿度/各层云量/风速由两端整点插值至区间中点。该展开不增加真实天气分辨率。',
        '- SeasonalNaive取前一天准确同刻的合格功率，不按行数位移。IrradianceGain拟合非负单一辐照系数。',
        '- WeatherRidge为无截距岭回归：GHI、GHI×温度/云量/湿度/风速、直射、散射、GHI×时刻正弦/余弦，共9个特征。训练集RMS缩放，固定平均平方误差加0.01倍系数平方和；NumPy求解。',
        '- 输出负值截为零；零辐照特征输出零。未确认交流装机容量，不编造功率上限。',
        '- 每折只使用该日零点之前且已结束的功率标签，参数及缩放单独保存。再分析天气并非当时已知输入，因此整个实验只标记retrospective_known_weather。', '',
        '## 限制与下一步', '',
        '- 当前样本只有约两周，完整日电量共同验证仅3天；不能据此确认季节泛化能力或稳定收益。',
        '- 没有用逆变器停机、限发和交流额定容量数据清洗功率。电量为采样功率积分估计，不是电能表累计值。',
        '- 后续接入并保留真正获取的预报批次，按实际获取时间选择输入，再评估相同预测提前量。可补充9月同源天气扩大样本。',
        '- 未写预测结果表、接入M4、触发优化或向设备下发指令。', '',
        '## 工件', '',
        '- `training.csv`：合格样本池，具体训练/测试边界以folds.json为准；不是把全部行一次拟合。',
        '- `aligned_all.csv`：整时间网格与排除原因；`backtest_points.csv`：576点完整回测网格。',
        '- `metrics.json`、`folds.json`：指标、每日训练边界、全部模型系数与缩放。',
        '- `manifest.json`：输入/代码/输出SHA-256；`inputs/`保留实测、天气、入库验证和本轮代码快照。',
        '- `power_comparison.png`：逐日功率曲线；`error_energy.png`：误差和完整日电量对比。', '',
        '## 来源', '',
        f"- 实测：授权查询 `https://vifa.hlszh.com/api/t_es_data:list` 的本地快照；SHA-256 `{manifest['pv_sha256']}`。",
        f"- 天气：已入库CSV，批次 `{manifest['weather_batch_id']}`，SHA-256 `{manifest['weather_sha256']}`。",
        '- [Open-Meteo历史天气定义](https://open-meteo.com/en/docs/historical-weather-api)。',
        '- [岭回归目标函数](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html)、'
        '[时间序列验证](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split)。', '']
    return '\n'.join(lines)


def run(pv_path, weather_path, verification_path, output):
    if output.exists():
        raise ValueError('output already exists; choose a new directory')
    verification = json.loads(verification_path.read_text())
    digest = sha(weather_path)
    if (verification.get('status') != 'completed' or verification.get('csv_sha256') != digest
            or verification.get('read_query', {}).get('es_sn') != 'ES02'
            or verification['read_query'].get('source_kind') != 'historical_reanalysis'):
        raise ValueError('weather source does not match completed ES02 import verification')
    raw = [json.loads(line) for line in pv_path.read_text().splitlines() if line.strip()]
    csv = pd.read_csv(weather_path, encoding='utf-8-sig')
    if list(csv.columns) != ['时间', *CSV_FIELDS] or len(csv) != verification['expected_rows']:
        raise ValueError('weather CSV columns/count do not match import')
    weather = csv.rename(columns=CSV_FIELDS).drop(columns='时间')
    weather.index = pd.DatetimeIndex(pd.to_datetime(csv['时间'])).tz_localize(pv_training.TZ)
    aligned = pv_training.align_weather(pv_training.prepare_pv(raw), weather)
    aligned['weather_batch_id'] = verification['source_batch_id']
    aligned['es_sn'] = 'ES02'
    training = aligned.loc[aligned.eligible]
    points, folds = pv_backtest.rolling_backtest(aligned)
    metrics = pv_backtest.summarize(points)
    if not metrics['irradiated_points']:
        raise ValueError('no common irradiated points for comparison')
    quality = {'pv_raw_rows': len(raw), 'pv_null_rows': sum(r['ac_solar_power'] is None for r in raw),
               'weather_rows': len(weather), 'quarter_grid_rows': len(aligned), 'eligible_quarters': len(training),
               'eligible_hours': len(training)//4, 'first_eligible': training.index.min().isoformat(),
               'last_eligible_end': (training.index.max()+pd.Timedelta(minutes=15)).isoformat(),
               'excluded_quarters_by_reason': {str(k): int(v) for k, v in aligned.loc[~aligned.eligible, 'rejection_reason'].value_counts().items()}}
    manifest = {'status': 'completed', 'evaluation_type': pv_backtest.EVALUATION_TYPE,
                'created_at': datetime.now(timezone.utc).isoformat(), 'station': 'ES02', 'timezone': pv_training.TZ,
                'pv_sha256': sha(pv_path), 'weather_sha256': digest, 'weather_batch_id': verification['source_batch_id'],
                'minimum_complete_training_days': 7, 'model_selection_on_test_set': False,
                'source_paths': {'pv': str(pv_path.resolve()), 'weather': str(weather_path.resolve())},
                'runtime': {'python': platform.python_version(), 'numpy': np.__version__, 'pandas': pd.__version__},
                'remote_reads_this_run': 0, 'database_writes': 0, 'operational_forecast': False}
    output.mkdir(parents=True, exist_ok=False)
    inputs = output/'inputs'
    inputs.mkdir()
    for path, name in [(pv_path, 'pv_snapshot.jsonl'), (weather_path, 'weather.csv'),
                       (verification_path, 'weather_import_verification.json')]:
        shutil.copyfile(path, inputs/name)
    code_dir = inputs/'code'
    code_dir.mkdir()
    for path in [Path(__file__), Path(pv_training.__file__), Path(pv_backtest.__file__)]:
        shutil.copyfile(path, code_dir/path.name)
    for name, frame in [('aligned_all', aligned), ('training', training), ('backtest_points', points)]:
        frame.to_csv(output/(name+'.csv'), encoding='utf-8-sig', index_label='target_time')
    for name, data in [('metrics', metrics), ('quality', quality), ('folds', folds)]:
        write_json(output/(name+'.json'), data)
    (output/'光伏回测报告.md').write_text(report_markdown(metrics, quality, folds, manifest), encoding='utf-8')
    manifest['files_sha256'] = {str(path.relative_to(output)): sha(path) for path in sorted(output.rglob('*')) if path.is_file()}
    write_json(output/'manifest.json', manifest)
    print(json.dumps({'output': str(output.resolve()), 'quality': quality, 'metrics': metrics}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pv-jsonl', type=Path, required=True)
    parser.add_argument('--weather-csv', type=Path, default=ROOT/'历史天气数据.csv')
    parser.add_argument('--import-verification', type=Path, default=ROOT/'m3/contracts/weather_history_import_verification.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.pv_jsonl, args.weather_csv, args.import_verification, args.output)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
