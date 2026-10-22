'use strict';
const fs = require('node:fs');
const path = require('node:path');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/dashboard_http_baseline.json'), 'utf8')).payload;
const copy = value => structuredClone(value);

function payload(version = 1) {
  const result = copy(base);
  const data = result.data;
  const timestamp = new Date(Date.UTC(2026, 9, 12, 4, version)).toISOString();
  delete data.source_errors;
  data.nodes.forEach(node => {
    node.name = node.node_type === 'grid' ? '电网' : node.node_type === 'load' ? '负载' :
      node.node_type === 'ess' ? (node.is_aggregate ? '储能柜' : node.sn + ' 储能') :
      node.node_type === 'meter' ? '光伏计量表' : '光伏系统';
  });
  data.realtime.forEach(row => {
    row.timestamp = timestamp;
    row.power = 20 + version;
    row.soc = 50 + version;
    if ('temperature_c' in row) row.temperature_c = 30 + version;
    if ('cabinet_timestamp' in row) row.cabinet_timestamp = timestamp;
    if ('pv_meter_timestamp' in row) row.pv_meter_timestamp = timestamp;
  });
  data.pv_inverters.forEach(row => {
    row.timestamp = timestamp;
    row.online = true;
    row.data_quality = 'good';
    row.input_power_kw = 10 + version;
    row.output_power_kw = 9 + version;
  });
  data.alerts.push({id:'derived:pv_efficiency:emu1',category:'pv_efficiency',device_sn:'emu1',
    fk_site_id:data.sites[0].id,fk_en_id:1102,txt:'光伏转换效率下降',start_time:timestamp});
  return result;
}

const scenarios = [
  'growatt', 'station_load:ES01', 'station_load:ES02', 'storage_cabinets',
  'storage_cabinets:ES01', 'storage_cabinets:ES02', 'pv_meter:ES02',
  'missing_realtime', 'missing_placeholder', 'history', 'all_sources',
];

function fail(input, kind) {
  const result = copy(input);
  const data = result.data;
  data.source_errors ||= {};
  const stationCode = node => data.es_list.find(station => String(station.id) === String(node.fk_es)).station_code;
  if (kind === 'all_sources') {
    return ['growatt','station_load:ES01','station_load:ES02','storage_cabinets','history']
      .reduce((value, next) => fail(value, next), result);
  }
  if (kind === 'growatt') {
    data.source_errors.growatt = 'simulated source failure';
    data.pv_inverters = data.pv_inverters.map(row => ({sn:row.sn,online:false,data_quality:'missing',
      timestamp:'',input_power_kw:null,output_power_kw:null,conversion_efficiency:null}));
    data.alerts = data.alerts.filter(alert => alert.category !== 'pv_efficiency');
  } else if (kind.startsWith('station_load:') || kind.startsWith('storage_cabinets')) {
    data.source_errors[kind] = 'simulated source failure';
    const [source, code] = kind.split(':');
    const affected = data.nodes.filter(node => (!code || stationCode(node) === code) &&
      node.node_type === (source === 'station_load' ? 'load' : 'ess'));
    for (const node of affected) {
      const row = data.realtime.find(row => row.fk_en === node.id);
      if (!row) continue;
      if (node.is_aggregate) Object.assign(row,{power:null,timestamp:'',power_source:'unavailable'});
      else { delete row.temperature_c; delete row.cabinet_timestamp; }
    }
    if (source === 'storage_cabinets') {
      for (const node of data.nodes.filter(node => node.node_type === 'meter' && (!code || stationCode(node) === code))) {
        const row = data.realtime.find(row => row.fk_en === node.id);
        if (row) row.pv_meter_timestamp = '';
      }
    }
    const affectedIds = new Set(affected.map(node => node.id));
    data.alerts = data.alerts.filter(alert => !affectedIds.has(alert.fk_en_id));
  } else if (kind === 'pv_meter:ES02') {
    data.source_errors[kind] = 'simulated missing meter time';
    data.realtime.find(row => row.fk_en === 1102).pv_meter_timestamp = '';
  } else if (kind === 'missing_realtime') {
    data.realtime = data.realtime.filter(row => row.fk_en !== 1010);
  } else if (kind === 'missing_placeholder') {
    Object.assign(data.realtime.find(row => row.fk_en === 1010),{
      data_quality:'missing',power:null,soc:null,timestamp:'',run_status:'offline',temperature_c:null,cabinet_timestamp:''});
  } else if (kind === 'history') {
    data.alert_history = {status:'error',records:[],message:'simulated history failure'};
  } else throw new Error('Unknown fixture: ' + kind);
  return result;
}

module.exports = {payload,fail,scenarios,copy};
