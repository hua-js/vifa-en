const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/M4优化调度控制台-线上版.html'), 'utf8');
const init = html.match(/async function initializeCurrent\(\)\{[\s\S]*?(?=\nasync function loadCurrent)/)[0];
const startup = html.match(/\ninitializeCurrent\(\)[\s\S]*?(?=\n\n<\/script>)/)[0];
function fixture({token = 'test-only-token', saved = 'station-2'} = {}) {
  let finishDigest;
  const digest = new Promise(resolve => { finishDigest = resolve; });
  const station = {value: 'station-1'};
  const calls = [];
  const buttons = ['station-1', 'station-2'].map(id => ({dataset: {stationId: id}, setAttribute(k,v) { this[k]=v; }}));
  const context = vm.createContext({token, viewCacheScope: null, location: {hostname: token ? 'example.test' : 'localhost'},
    crypto: {subtle: {digest: () => digest}}, TextEncoder,
    sessionStorage: {getItem: () => saved}, $: () => station,
    document: {querySelectorAll: () => buttons},
    loadProject: async () => {
      station.value = saved || 'station-1';
      buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.stationId === station.value)));
    },
    loadCurrent: () => calls.push(['plan',station.value]),
    refreshReferenceSources: () => calls.push(['logs',station.value])});
  vm.runInContext(init + '\n' + startup, context);
  return {station, calls, buttons, finishDigest};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
test('authenticated startup waits for saved station before querying logs', async () => {
  const f = fixture();
  assert.deepEqual(f.calls, [], 'no default-station reads before digest completes');
  f.finishDigest(new Uint8Array([1]).buffer); await settle();
  assert.deepEqual(f.calls, [['plan','station-2'],['logs','station-2']]);
  assert.equal(f.buttons[1]['aria-pressed'], 'true');
  assert.equal(f.buttons[0]['aria-pressed'], 'false');
});
test('local startup and default station still initialize once', async () => {
  const f = fixture({token:'',saved:null}); await settle();
  assert.deepEqual(f.calls, [['plan','station-1'],['logs','station-1']]);
});
