'use strict';
const fs = require('node:fs');
const path = require('node:path');
for (const lane of ['client', 'client-v2']) {
  const dir = path.join(__dirname, 'dist', lane);
  fs.mkdirSync(dir, {recursive: true});
  fs.copyFileSync(path.join(__dirname, 'client-entry.js'), path.join(dir, 'index.js'));
}

// Concrete dependency baseline validated on NocoBase 2.2.2; do not write ranges here.
const pkg = require('./package.json');
const externalVersion = {};
for (const name of ['@nocobase/server', '@nocobase/client']) {
  const version = pkg.devDependencies[name];
  if (!/^2\.2\.\d+$/.test(version)) throw new Error(`Invalid concrete dependency version: ${name}`);
  externalVersion[name] = version;
}
fs.writeFileSync(path.join(__dirname, 'dist', 'externalVersion.js'),
  "'use strict';\nmodule.exports = " + JSON.stringify(externalVersion, null, 2) + ';\n');
