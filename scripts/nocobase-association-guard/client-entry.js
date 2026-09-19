// This server-side policy has no client UI. NocoBase still loads its client entry.
(function (root, factory) {
  if (typeof exports === 'object' && typeof module === 'object') {
    module.exports = factory(require('@nocobase/client'));
  } else if (typeof define === 'function' && define.amd) {
    define('@vifa/plugin-association-read-guard', ['@nocobase/client'], factory);
  } else {
    root['@vifa/plugin-association-read-guard'] = factory(root['@nocobase/client']);
  }
})(typeof self !== 'undefined' ? self : globalThis, function (client) {
  'use strict';
  class AssociationReadGuardClient extends client.Plugin {
    async load() {}
  }
  return { __esModule: true, default: AssociationReadGuardClient };
});
