'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {payload,copy} = require('./availability_fixture.cjs');
const html = fs.readFileSync(path.join(__dirname,'../web/dashboard_energy.html'),'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const functionNames = ['stationCode','isAnomalyAlert','isolateCustomerData','retainAvailableSourceData',
  'nodeOnline','summarizePvInverters','fetchData'];
const source = [
  script.match(/^const TARGET_TOPOLOGY =[\s\S]*?^\}\);/m)[0],
  script.match(/^const ANOMALY_CATEGORIES =.*$/m)[0],
  ...functionNames.map(name => script.match(new RegExp('^(?:async )?function '+name+'\\([^]*?^\\}', 'm'))[0]),
].join('\n');

function fixture(fetchImpl, {cold = false} = {}) {
  const timers = new Map();
  const refreshInfo = {textContent:'↻ 12:00:00 · 30s'};
  const notices = [];
  const renders = [];
  let timerId = 0;
  const sandbox = {
    fetchInFlight:false,TOKEN:null,DEMO_MODE:false,
    API_URL:'/dashboard_energy_api',DEMO_DATA_URL:'result.json',
    API_REQUEST_TIMEOUT_MS:120000,REFRESH_INTERVAL_MS:30000,AbortController,
    allData:null,lastSuccessfulData:null,
    fetch:fetchImpl,
    render(){renders.push(copy(sandbox.allData));},
    renderDataUnavailable(...args){notices.push(args);},showToast(...args){notices.push(args);},
    document:{getElementById:()=>refreshInfo},console:{error(){}},
    setTimeout(fn,delay){timers.set(++timerId,{fn,delay});return timerId;},
    clearTimeout(id){timers.delete(id);},
  };
  vm.createContext(sandbox);
  vm.runInContext(source,sandbox);
  const previous = cold ? null : sandbox.isolateCustomerData(payload(1).data);
  sandbox.allData = sandbox.lastSuccessfulData = previous;
  return {sandbox,timers,previous,refreshInfo,notices,renders};
}

module.exports = {fixture,script};
