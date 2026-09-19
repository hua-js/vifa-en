'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
for (const lane of ['client','client-v2']) {
  test(`${lane} entry registers an inert NocoBase client plugin`, async () => {
    const root = path.resolve(__dirname, '..');
    assert.ok(fs.existsSync(path.join(root, lane + '.js')), 'client lane marker missing');
    const source = fs.readFileSync(path.join(root, 'dist', lane, 'index.js'), 'utf8');
    class Plugin { constructor(app) { this.app = app; } }
    const self = {'@nocobase/client': {Plugin}};
    vm.runInNewContext(source, {self});
    const exported = self['@vifa/plugin-association-read-guard'];
    assert.equal(typeof exported.default, 'function');
    const app = Object.freeze({});
    const instance = new exported.default(app);
    assert.ok(instance instanceof Plugin);
    await instance.load();
    assert.equal(instance.app, app);
  });
}

test('AMD loader resolves the declared NocoBase client dependency', async () => {
  class Plugin {}
  let module;
  const define = (name, dependencies, factory) => {
    assert.equal(name, '@vifa/plugin-association-read-guard');
    assert.deepEqual(Array.from(dependencies), ['@nocobase/client']);
    module = factory({Plugin});
  };
  define.amd = {};
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../dist/client/index.js'), 'utf8'), {self: {}, define});
  const instance = new module.default();
  assert.ok(instance instanceof Plugin);
  await instance.load();
});
