'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
test('release includes loadable concrete dependency versions for NocoBase UI checks', () => {
  const pkg = require(path.join(root, 'package.json'));
  const metadata = path.join(root, 'dist/externalVersion.js');
  assert.ok(fs.existsSync(metadata), 'NocoBase returns false and empty dependency rows without dist/externalVersion.js');
  assert.ok(pkg.files.includes('dist/externalVersion.js'));
  const versions = require(metadata);
  for (const name of ['@nocobase/server', '@nocobase/client']) {
    assert.match(versions[name], /^2\.2\.\d+$/, `${name} must record a concrete baseline version, not a semver range`);
    assert.equal(versions[name], pkg.devDependencies[name]);
    assert.ok(pkg.peerDependencies[name]);
  }
});
