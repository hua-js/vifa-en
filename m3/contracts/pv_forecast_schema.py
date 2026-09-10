"""PostgreSQL design contract for weather inputs and photovoltaic forecasts.

This is a deployment contract, not a change to the live M3/M4 forecasting flow.
"""

VERSION = 1
BASE_URL = 'https://vifa.hlszh.com'


def field(name, pg_type, title, *, nullable=False, default=None, description=None):
    item = {'name': name, 'type': pg_type, 'title': title, 'nullable': nullable}
    if default is not None:
        item['default'] = default
    if description:
        item['description'] = description
    return item


def audit_fields():
    return [
        field('createdAt', 'timestamptz', '创建时间', nullable=True),
        field('updatedAt', 'timestamptz', '更新时间', nullable=True),
    ]


def index(name, fields, unique=False):
    return {'name': name, 'fields': fields, 'unique': unique}


def finite_range(name, low=None, high=None):
    terms = [f'"{name}" > \'-Infinity\'::numeric', f'"{name}" < \'Infinity\'::numeric']
    if low is not None:
        terms.append(f'"{name}" >= {low}')
    if high is not None:
        terms.append(f'"{name}" <= {high}')
    return ' AND '.join(terms)


WEATHER_FIELDS = [
    field('id', 'bigint', '主键'),
    field('es_sn', 'text', '电站编号', description='与 t_es_data.es_sn 对齐；当前为 ES02。'),
    field('source_kind', 'text', '天气来源类型', description='historical_reanalysis 或 forecast；历史再分析不得冒充当时的天气预报。'),
    field('provider', 'text', '天气提供方', default='open_meteo'),
    field('source_batch_id', 'text', '天气批次标识', description='来源文件或完整API响应及站点、坐标、时区、来源类型等身份元数据的SHA-256。相同批次重复导入使用同一标识。'),
    field('weather_time', 'timestamptz', '天气时间标签', description='辐照为该时刻前一小时均值，降雨为前一小时累计；温度、湿度、云量和风速为该整点瞬时值。'),
    field('interval_minutes', 'integer', '天气间隔（分钟）', default=60),
    field('latitude', 'numeric(9,6)', '请求纬度'),
    field('longitude', 'numeric(9,6)', '请求经度'),
    field('timezone', 'text', '来源时区', default='Asia/Shanghai'),
    field('issued_at', 'timestamptz', '预报发布时间', nullable=True, description='未知时保持NULL；导入历史CSV不得伪造发布时间。'),
    field('fetched_at', 'timestamptz', '原始数据获取时间', nullable=True, description='未来预报必须保留实际获取时间；现有CSV原下载时间未保存，保持NULL。'),
    field('temperature_c', 'numeric(7,3)', '气温（℃）', nullable=True),
    field('relative_humidity_pct', 'numeric(6,3)', '相对湿度（%）', nullable=True),
    field('cloud_cover_pct', 'numeric(6,3)', '总云量（%）', nullable=True),
    field('cloud_cover_low_pct', 'numeric(6,3)', '低层云量（%）', nullable=True),
    field('cloud_cover_mid_pct', 'numeric(6,3)', '中层云量（%）', nullable=True),
    field('cloud_cover_high_pct', 'numeric(6,3)', '高层云量（%）', nullable=True),
    field('ghi_wm2', 'numeric(12,3)', '水平面总辐照（W/m²）', nullable=True),
    field('direct_horizontal_wm2', 'numeric(12,3)', '水平面直射辐照（W/m²）', nullable=True, description='对应Open-Meteo direct_radiation，不是DNI。'),
    field('dhi_wm2', 'numeric(12,3)', '水平面散射辐照（W/m²）', nullable=True),
    field('precipitation_mm', 'numeric(10,3)', '前一小时降雨量（mm）', nullable=True),
    field('wind_speed_kmh', 'numeric(10,3)', '10米风速（km/h）', nullable=True, description='保留原CSV单位；调用需要m/s的模型时除以3.6。'),
    field('quality_status', 'text', '数据质量', default='unreviewed'),
    field('source_metadata', 'jsonb', '来源元数据', description='保存文件名、文件SHA-256、请求模型选择、实际模型/网格信息（未知不填）、单位和时间语义版本；不得存凭据。'),
    field('content_hash', 'text', '本行内容哈希'),
    *audit_fields(),
]

RUN_FIELDS = [
    field('id', 'bigint', '主键'),
    field('run_id', 'text', '预测批次UUID'),
    field('es_sn', 'text', '电站编号'),
    field('run_kind', 'text', '运行用途', description='operational 为实际预测；backtest 为历史回测或同输入回算。'),
    field('as_of', 'timestamptz', '可用信息截止时间', description='每条实际使用的历史功率采样、天气发布时间/获取时间都必须不晚于此时间；事后天气回测必须另行标明。'),
    field('forecast_start', 'timestamptz', '预测区间开始'),
    field('forecast_end', 'timestamptz', '预测区间结束（不含）'),
    field('interval_minutes', 'integer', '预测间隔（分钟）', default=15),
    field('expected_points', 'integer', '应有预测点数'),
    field('status', 'text', '运行状态', default='queued'),
    field('model_name', 'text', '模型名称', nullable=True),
    field('model_version', 'text', '模型版本', nullable=True),
    field('model_config', 'jsonb', '模型参数'),
    field('training_start', 'timestamptz', '实际训练数据开始', nullable=True),
    field('training_end', 'timestamptz', '实际训练数据结束（不含）', nullable=True),
    field('pv_history_end', 'timestamptz', '最新实测功率采样时间', nullable=True),
    field('weather_batch_id', 'text', '本次使用的天气批次', nullable=True, description='引用energy_weather_points.source_batch_id；它是一批多行，不是唯一外键。写入服务校验站点和完整覆盖。'),
    field('weather_source_kind', 'text', '本次天气来源类型', nullable=True),
    field('source_manifest', 'jsonb', '输入来源与快照清单', description='包含天气批次/来源类型/实际使用行哈希、实际使用的聚合光伏输入快照或不可变快照引用、聚合与缺失规则；仅窗口和哈希不能还原输入。'),
    field('evaluation_metrics', 'jsonb', '评估指标与口径', nullable=True, description='保存评估窗口、预测步长、白天/全天、样本覆盖率、MAE/RMSE等；无评估保持NULL。'),
    field('bounds_coverage_pct', 'numeric(6,3)', '预测区间名义覆盖率（%）', nullable=True),
    field('generated_at', 'timestamptz', '预测生成完成时间', nullable=True),
    field('error_code', 'text', '失败原因代码', nullable=True),
    field('content_hash', 'text', '本批预测内容哈希', nullable=True),
    *audit_fields(),
]

POINT_FIELDS = [
    field('id', 'bigint', '主键'),
    field('run_pk', 'bigint', '预测批次主键'),
    field('target_time', 'timestamptz', '15分钟区间开始时间'),
    field('horizon_step', 'integer', '预测步数（从1开始）'),
    field('forecast_kw', 'numeric(14,6)', '区间平均交流光伏功率（kW）'),
    field('raw_forecast_kw', 'numeric(14,6)', '约束处理前预测功率（kW）', nullable=True),
    field('baseline_kw', 'numeric(14,6)', '同批次七日参考功率（kW）', nullable=True),
    field('lower_kw', 'numeric(14,6)', '预测区间下界（kW）', nullable=True),
    field('upper_kw', 'numeric(14,6)', '预测区间上界（kW）', nullable=True),
    field('is_clipped', 'boolean', '是否经过边界截断', default=False),
    *audit_fields(),
]

weather_numeric = [f for f in WEATHER_FIELDS if f['type'].startswith('numeric')]
weather_checks = [
    ('ewp_station_ck', 'length(trim(es_sn)) > 0'),
    ('ewp_source_kind_ck', "source_kind IN ('historical_reanalysis', 'forecast')"),
    ('ewp_quality_ck', "quality_status IN ('unreviewed', 'valid', 'incomplete', 'invalid')"),
    ('ewp_identity_ck', "length(trim(provider)) > 0 AND source_batch_id ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'"),
    ('ewp_hour_ck', "interval_minutes = 60 AND weather_time = date_trunc('hour', weather_time, 'UTC')"),
    ('ewp_availability_ck', "(source_kind <> 'forecast' OR fetched_at IS NOT NULL) AND (issued_at IS NULL OR fetched_at IS NULL OR issued_at <= fetched_at)"),
    ('ewp_metadata_ck', "jsonb_typeof(source_metadata) = 'object' AND length(trim(timezone)) > 0"),
]
for f in weather_numeric:
    name = f['name']
    low, high = (None, None) if name == 'temperature_c' else (0, None)
    if name.endswith('_pct'):
        high = 100
    if name == 'latitude':
        low, high = -90, 90
    if name == 'longitude':
        low, high = -180, 180
    weather_checks.append((f'ewp_{name}_ck', finite_range(name, low, high)))

TABLES = [
    {
        'name': 'energy_weather_points', 'title': '光伏预测天气数据',
        'description': '按电站、来源类型和批次保存小时天气，兼容现有历史CSV和未来天气预报，保留来源身份与时间语义。',
        'fields': WEATHER_FIELDS,
        'indexes': [
            index('ewp_batch_time_uq', ['es_sn', 'source_kind', 'source_batch_id', 'weather_time'], True),
            index('ewp_station_time_idx', ['es_sn', 'source_kind', 'weather_time']),
            index('ewp_station_fetched_idx', ['es_sn', 'source_kind', 'fetched_at']),
        ],
        'checks': weather_checks,
    },
    {
        'name': 'energy_pv_forecast_runs', 'title': '光伏功率预测批次',
        'description': '记录模型、训练窗口、天气批次、信息截止时间和发布状态，支持原始预测追溯与离线回测。',
        'fields': RUN_FIELDS,
        'indexes': [
            index('epfr_run_id_uq', ['run_id'], True),
            index('epfr_station_status_asof_idx', ['es_sn', 'run_kind', 'status', 'as_of']),
        ],
        'checks': [
            ('epfr_identity_ck', "length(trim(es_sn)) > 0 AND run_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"),
            ('epfr_kind_ck', "run_kind IN ('operational', 'backtest')"),
            ('epfr_weather_kind_ck', "weather_source_kind IS NULL OR weather_source_kind IN ('historical_reanalysis', 'forecast')"),
            ('epfr_status_ck', "status IN ('queued', 'running', 'completed', 'failed')"),
            ('epfr_window_ck', "interval_minutes = 15 AND expected_points > 0 AND forecast_end > forecast_start AND as_of <= forecast_start AND forecast_end = forecast_start + expected_points * interval '15 minutes' AND mod(extract(epoch FROM forecast_start), 900) = 0"),
            ('epfr_training_ck', '(training_start IS NULL AND training_end IS NULL) OR (training_start IS NOT NULL AND training_end IS NOT NULL AND training_start < training_end AND training_end <= as_of)'),
            ('epfr_history_ck', 'pv_history_end <= as_of'),
            ('epfr_hash_ck', "(weather_batch_id IS NULL OR weather_batch_id ~ '^[0-9a-f]{64}$') AND (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$')"),
            ('epfr_json_ck', "jsonb_typeof(model_config) = 'object' AND jsonb_typeof(source_manifest) = 'object' AND (evaluation_metrics IS NULL OR jsonb_typeof(evaluation_metrics) = 'object')"),
            ('epfr_coverage_ck', finite_range('bounds_coverage_pct', 0.001, 99.999)),
            ('epfr_generated_ck', 'generated_at >= as_of'),
            ('epfr_completed_ck', "status <> 'completed' OR (model_name IS NOT NULL AND length(trim(model_name)) > 0 AND model_version IS NOT NULL AND length(trim(model_version)) > 0 AND weather_batch_id IS NOT NULL AND weather_source_kind IS NOT NULL AND generated_at IS NOT NULL AND content_hash IS NOT NULL AND error_code IS NULL)"),
            ('epfr_operational_ck', "status <> 'completed' OR run_kind <> 'operational' OR weather_source_kind = 'forecast'"),
            ('epfr_failed_ck', "status <> 'failed' OR error_code IS NOT NULL"),
        ],
    },
    {
        'name': 'energy_pv_forecast_points', 'title': '光伏功率预测结果',
        'description': '每批次每15分钟一条平均交流功率；通过run_pk关联批次，实测继续使用t_es_data，缺失预测不填零。',
        'fields': POINT_FIELDS,
        'indexes': [
            index('epfp_run_time_uq', ['run_pk', 'target_time'], True),
            index('epfp_run_step_uq', ['run_pk', 'horizon_step'], True),
            index('epfp_target_time_idx', ['target_time']),
        ],
        'relationship': {'name': 'run', 'title': '预测批次', 'target': 'energy_pv_forecast_runs', 'foreignKey': 'run_pk', 'targetKey': 'id', 'onDelete': 'RESTRICT'},
        'checks': [
            ('epfp_time_step_ck', 'horizon_step > 0 AND mod(extract(epoch FROM target_time), 900) = 0'),
            ('epfp_forecast_ck', finite_range('forecast_kw', 0)),
            ('epfp_raw_ck', finite_range('raw_forecast_kw')),
            ('epfp_baseline_ck', finite_range('baseline_kw', 0)),
            ('epfp_lower_ck', finite_range('lower_kw', 0)),
            ('epfp_upper_ck', finite_range('upper_kw', 0)),
            ('epfp_bounds_ck', '(lower_kw IS NULL AND upper_kw IS NULL) OR (lower_kw IS NOT NULL AND upper_kw IS NOT NULL AND lower_kw <= forecast_kw AND forecast_kw <= upper_kw)'),
        ],
    },
]

# PostgreSQL accepts +/-infinity timestamps; require finite real timestamps.
# Nullable fields remain nullable because CHECK(NULL) is accepted by PostgreSQL.
for table in TABLES:
    time_fields = [f['name'] for f in table['fields'] if f['type'] == 'timestamptz']
    prefix = {'energy_weather_points': 'ewp', 'energy_pv_forecast_runs': 'epfr', 'energy_pv_forecast_points': 'epfp'}[table['name']]
    table['checks'].append((prefix + '_finite_times_ck', ' AND '.join(f'isfinite("{name}")' for name in time_fields)))
