#!/usr/bin/env python3
"""Render standalone scientific figures from the saved backtest bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

COLORS = {'actual_kw': '#182e44', 'baseline_kw': '#c98039', 'gain_kw': '#288b80', 'ridge_kw': '#426dcc'}
LABELS = {'actual_kw': '实测功率', 'baseline_kw': '昨日同刻', 'gain_kw': '辐照比例', 'ridge_kw': 'WeatherRidge'}
MODELS = {'SeasonalNaive': '昨日同刻', 'IrradianceGain': '辐照比例', 'WeatherRidge': 'WeatherRidge'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plot(directory):
    targets = [directory/'power_comparison.png', directory/'error_energy.png']
    if any(p.exists() for p in targets):
        raise ValueError('figures already exist')
    manifest = json.loads((directory/'manifest.json').read_text())
    for relative, expected in manifest['files_sha256'].items():
        if sha(directory/relative) != expected:
            raise ValueError('bundle checksum mismatch: '+relative)
    for candidate in ['/System/Library/Fonts/PingFang.ttc', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf']:
        if Path(candidate).exists():
            font_manager.fontManager.addfont(candidate)
            font = font_manager.FontProperties(fname=candidate).get_name()
            break
    else:
        raise ValueError('Chinese font unavailable')
    plt.rcParams.update({'font.family': font, 'font.size': 11, 'axes.unicode_minus': False,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.edgecolor': '#c5cdd4',
        'axes.labelcolor': '#34495e', 'xtick.color': '#506174', 'ytick.color': '#506174',
        'grid.color': '#e4e9ef', 'grid.alpha': .85, 'figure.facecolor': '#fafbfd', 'axes.facecolor': '#ffffff'})
    data = pd.read_csv(directory/'backtest_points.csv')
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('target_time'), utc=True)).tz_convert('Asia/Shanghai')
    metrics = json.loads((directory/'metrics.json').read_text())
    days = list(data.groupby(data.index.normalize()))
    fig, axes = plt.subplots((len(days)+1)//2, 2, figsize=(15, 10.7), squeeze=False, sharey=True)
    for ax, (day, rows) in zip(axes.flat, days):
        hours = rows.index.hour + rows.index.minute/60
        for column in COLORS:
            ax.plot(hours, rows[column], color=COLORS[column], label=LABELS[column],
                    linewidth=1.9 if column in ('actual_kw', 'ridge_kw') else 1.25,
                    linestyle='--' if column == 'baseline_kw' else '-', alpha=.95)
        shared = int(rows.score_shared.sum())
        ax.set_title(day.strftime('%m月%d日')+f'  ·  共同评分 {shared}/96 点', loc='left', fontsize=12, pad=11)
        ax.set_xlim(0, 24)
        ax.set_ylim(bottom=0)
        ax.set_xticks([0, 6, 12, 18, 24])
        ax.set_xlabel('北京时间 / 时')
        ax.set_ylabel('平均交流功率 / kW')
        ax.grid(axis='y')
    for ax in list(axes.flat)[len(days):]:
        ax.set_visible(False)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, ncol=4, loc='upper center', bbox_to_anchor=(.5, .943), frameon=False)
    fig.suptitle('电站2 · 光伏功率逐日回测', x=.06, y=.991, ha='left', fontsize=20, weight='bold')
    fig.text(.06, .952, '2026年8月26–31日  |  每15分钟一点  |  逐日扩展训练窗口', fontsize=11, color='#506174')
    fig.text(.06, .025, '天气模型使用事后再分析天气；曲线断线表示缺失或不合格输入。小时天气展开不代表真实15分钟天气分辨率。',
             fontsize=10, color='#506174')
    fig.subplots_adjust(top=.872, bottom=.095, left=.07, right=.98, hspace=.48, wspace=.16)
    fig.savefig(targets[0], dpi=170)
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(13, 9.2))
    names = list(MODELS)
    pos = np.arange(len(names))
    for metric, shift, color, label in [('mae_kw', -.17, '#426dcc', 'MAE'), ('rmse_kw', .17, '#8ea7d7', 'RMSE')]:
        heights = [metrics['models'][name]['irradiated'][metric] for name in names]
        bars = axes[0].bar(pos+shift, heights, .29, label=label, color=color)
        axes[0].bar_label(bars, fmt='%.1f', padding=4, fontsize=10)
    axes[0].set_xticks(pos, [MODELS[n] for n in names])
    axes[0].set_ylim(0, max(metrics['models'][n]['irradiated']['rmse_kw'] for n in names)*1.22)
    axes[0].set_ylabel('误差 / kW（越低越好）')
    axes[0].set_title(f"有辐照时段功率误差 · 同一组{metrics['irradiated_points']}点，GHI > 20 W/m²", loc='left', pad=15)
    axes[0].legend(frameon=False)
    axes[0].grid(axis='y')
    axes[0].set_axisbelow(True)
    full = [d for d in metrics['daily'] if d['full_day_comparable']]
    positions = np.arange(len(full))
    for i, (name, key) in enumerate([('实测估计', None), *[(MODELS[n], n) for n in names]]):
        heights = [d['actual_kwh'] if key is None else d['models'][key]['predicted_kwh'] for d in full]
        color = list(COLORS.values())[i]
        bars = axes[1].bar(positions+(i-1.5)*.19, heights, .17, label=name, color=color)
        axes[1].bar_label(bars, fmt='%.0f', padding=4, fontsize=9)
    axes[1].set_xticks(positions, [d['date'][5:] for d in full])
    axes[1].set_ylabel('日发电量估计 / kWh')
    axes[1].set_ylim(0, max([d['actual_kwh'] for d in full]+[d['models'][n]['predicted_kwh'] for d in full for n in names])*1.22 if full else 1)
    axes[1].set_title(f'完整日期能量 · 仅比较{len(full)}个共同覆盖96点的日期', loc='left', pad=15)
    axes[1].legend(ncol=4, frameon=False, loc='upper right')
    axes[1].grid(axis='y')
    axes[1].set_axisbelow(True)
    fig.suptitle('电站2 · 误差与日发电量对比', x=.075, y=.983, ha='left', fontsize=20, weight='bold')
    fig.text(.075, .94, '首轮离线验证  |  固定模型参数  |  再分析天气条件，不代表上线预报精度', color='#506174')
    fig.text(.075, .025, '日发电量由采样功率积分估计，非电能表累计值；缺点日不外推全天电量。', color='#506174', fontsize=10)
    fig.subplots_adjust(top=.85, bottom=.095, left=.09, right=.98, hspace=.49)
    fig.savefig(targets[1], dpi=170)
    plt.close(fig)
    code = directory/'inputs/code'/Path(__file__).name
    shutil.copyfile(__file__, code)
    manifest['plot_runtime'] = {'matplotlib': matplotlib.__version__, 'numpy': np.__version__, 'pandas': pd.__version__, 'font': font}
    for path in [*targets, code]:
        manifest['files_sha256'][str(path.relative_to(directory))] = sha(path)
    (directory/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({'figures': [str(p.resolve()) for p in targets], 'matplotlib': matplotlib.__version__}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    plot(parser.parse_args().directory)
