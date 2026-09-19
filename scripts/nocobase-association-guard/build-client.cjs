'use strict';
const fs = require('node:fs');
const path = require('node:path');
for (const lane of ['client', 'client-v2']) {
  const dir = path.join(__dirname, 'dist', lane);
  fs.mkdirSync(dir, {recursive: true});
  fs.copyFileSync(path.join(__dirname, 'client-entry.js'), path.join(dir, 'index.js'));
}
