#!/usr/bin/env python3
"""Render a static power/irradiance figure from a verified operational bundle."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import dates as mdates, font_manager
import numpy as np
import pandas as pd


def plot(directory):
    output = directory/'forecast_curve.png'
    if output.exists():
        raise ValueError('figure already exists')
    manifest = json.loads((directory/'manifest.json').read_text())
    for relative, digest in manifest['files_sha256'].items():
        if hashlib.sha256((directory/relative).read_bytes()).hexdigest() != digest:
            raise ValueError('bundle checksum mismatch')
    run = json.loads((directory/'run.json').read_text())
    report = json.loads((directory/'publication_verification.json').read_text())
    if report['status'] != 'completed' or report['run_id'] != run['run_id'] or report['content_hash'] != run['content_hash']:
        raise ValueError('matching publication verification required')
    points = json.loads((directory/'points.json').read_text())
    values = np.array([float(p['forecast_kw']) for p in points])
    times = pd.DatetimeIndex([pd.Timestamp(p['target_time']) for p in points])
    edges = mdates.date2num([*times.to_pydatetime(), pd.Timestamp(run['forecast_end']).to_pydatetime()])
    weather = pd.read_csv(directory/'forecast_weather.csv')
    font_path = next((Path(path) for path in ['/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'] if Path(path).exists()), None)
    if font_path is None:
        raise ValueError('Chinese font unavailable')
    font_manager.fontManager.addfont(str(font_path))
    plt.rcParams.update({'font.family': font_manager.FontProperties(fname=str(font_path)).get_name(),
        'font.size': 11, 'axes.unicode_minus': False, 'axes.spines.top': False, 'axes.spines.right': False,
        'figure.facecolor': '#f7f9fc', 'axes.facecolor': '#ffffff', 'axes.edgecolor': '#ccd4df',
        'axes.labelcolor': '#40516a', 'xtick.color': '#506174', 'ytick.color': '#506174'})
    fig, axes = plt.subplots(2, 1, figsize=(13, 7.4), gridspec_kw={'height_ratios': [2.5, 1]}, sharex=True)
    axes[0].stairs(values, edges, color='#2875ce', linewidth=2)
    axes[0].stairs(values, edges, color='#2875ce', fill=True, alpha=.12)
    axes[0].set_ylabel('十五分钟平均交流功率 / kW')
    axes[0].set_ylim(0, max(values.max()*1.3, 1))
    peak = int(np.argmax(values))
    axes[0].annotate(f'峰值 {values[peak]:.1f} kW\n{times[peak]:%m-%d %H:%M}',
        (edges[peak], values[peak]), xytext=(15, 25), textcoords='offset points', color='#245d99',
        arrowprops={'arrowstyle': '-', 'color': '#7ea2ce'}, fontsize=11)
    axes[1].stairs(weather.ghi_wm2, edges, color='#c58935', fill=True, alpha=.3)
    axes[1].stairs(weather.ghi_wm2, edges, color='#b78034', linewidth=1.2)
    axes[1].set_ylabel('短波辐照 / W/m²')
    axes[1].set_xlabel('北京时间')
    for ax in axes:
        ax.grid(axis='y', color='#e5eaf1')
        ax.set_axisbelow(True)
        ax.set_xlim(edges[0], edges[-1])
    axes[1].xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 3, 6, 9, 12, 15, 18, 21], tz=times.tz))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%m-%d\n%H:%M', tz=times.tz))
    fig.suptitle('电站2 · 未来24小时光伏功率预测', x=.085, y=.97, ha='left', fontsize=20, color='#20344c')
    fig.text(.085, .915, f'{times[0]:%m月%d日 %H:%M} — {pd.Timestamp(run["forecast_end"]):%m月%d日 %H:%M}'
        f'   |   96点已入库   |   预计发电量 {values.sum()*.25:,.1f} kWh', color='#40516a', fontsize=12)
    fig.text(.085, .052, f'WeatherRidge · 训练截止{pd.Timestamp(run["training_end"]):%Y-%m-%d %H:%M} · 使用实际获取的天气预报；预测精度待后续实测验证。', fontsize=10, color='#586980')
    fig.text(.085, .021, '小时天气展开到十五分钟；未估计置信区间，未设置未经确认的装机功率上限。', fontsize=10, color='#586980')
    fig.subplots_adjust(top=.85, bottom=.17, left=.085, right=.965, hspace=.18)
    fig.savefig(output, dpi=170)
    plt.close(fig)
    print(str(output.resolve()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    plot(parser.parse_args().directory)
