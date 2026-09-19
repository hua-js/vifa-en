'use strict';
const {Plugin} = require('@nocobase/server');
const {createGuard} = require('./guard');
const policy = require('./policy.json');
class AssociationReadGuardPlugin extends Plugin {
  async load() {
    this.app.resourceManager.use(createGuard(policy), {
      tag: 'vifaAssociationReadGuard', after: 'setCurrentRole', before: 'acl',
    });
  }
}
module.exports = AssociationReadGuardPlugin;
