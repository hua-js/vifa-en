'use strict';
function snapshot(station, date) {
  const start = Date.parse(date + 'T00:00:00+08:00');
  const points = Array.from({length:96}, (_,i) => ({timestamp:new Date(start+i*900000).toISOString(),
    load_forecast_kw:100, pv_forecast_kw:0, buy_price_per_kwh:1, tariff_period:'ping'}));
  const plan = points.map(p => ({timestamp:p.timestamp, mode:'idle', target_power_kw:0,
    grid_import_kw:100, grid_export_kw:0, expected_soc_pct:50}));
  const candidate = {plan_version:'ems/v1', plan, metrics:{energy_cost:2400, cycle_cost:0}};
  const request = {station_id:station, plan_start_at:points[0].timestamp, points,
    source_versions:{configuration:'saved-config', controls:'saved-controls',
      baseline_policy:'ems-demand-soc-duration-v9-initial-soc', load_admission_policy:'complete-forecast-v1'},
    capability:{energy_capacity_kwh:1000, max_charge_kw:100, max_discharge_kw:100, initial_soc_pct:50},
    constraints:{soc_min_pct:10, soc_max_pct:90, grid_import_limit_kw:720, demand_limit_kw:500}};
  const comparison = {schema_version:'m4-daily-comparison-v1', station_id:station,
    usage:'preview_only', dispatch_status:'not_dispatched', status:'ems', date,
    start_at:points[0].timestamp, end_at:new Date(start+24*60*60*1000).toISOString(), controls_version:'saved-controls',
    baseline_policy_version:'ems-demand-soc-duration-v9-initial-soc', baseline:candidate, recommended:candidate,
    baseline_cost_yuan:2400, recommended_source:'ems'};
  const record = {station_id:station, run_id:'fixture-run', finished_at:points[0].timestamp,
    input_summary:{configuration_version:'saved-config'}, daily_comparison:comparison};
  return {station_id:station, run_id:record.run_id, status:'completed', request,
    baseline:{station_id:station, controls_version:'saved-controls', controller_version:request.source_versions.baseline_policy,
      basis:'ems_rule_simulation', schedule:[{start_time:'00:00', end_time:'24:00', repeat:'daily', mode:'charge', power_kw:0}]},
    result:{schema_version:'m4-decision-results-v1', station_id:station, dispatch_status:'not_dispatched', record}};
}
module.exports = {snapshot};
