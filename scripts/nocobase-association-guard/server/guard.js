'use strict';
function empty(value) { return value == null || value === '' || (Array.isArray(value) && value.length === 0); }
function scalarFields(value, fields) {
  if (empty(value)) return true;
  const list = Array.isArray(value) ? value : typeof value === 'string' ? value.split(',') : null;
  return !!list && list.every(v => typeof v === 'string' && fields.has(v.trim()));
}
function invalidOptions(obj, fields) {
  if (!obj || typeof obj !== 'object') return false;
  return Object.entries(obj).some(([key,value]) => {
    if (/^appends(?:\[.*\])?$/.test(key)) return !empty(value);
    if (/^fields(?:\[.*\])?$/.test(key)) return !scalarFields(value, fields);
    return value && typeof value === 'object' && invalidOptions(value, fields);
  });
}
function createGuard(policy) {
  const roles = new Set(policy.roles);
  const collections = Object.entries(policy.collections).map(([name, fields]) => [name, new Set(fields)]);
  return async function associationReadGuard(ctx, next) {
    if (!ctx.state?.currentUser || !ctx.state?.currentRoles?.some(r => roles.has(r))) return next();
    const resource = ctx.action?.resourceName || '';
    let path;
    try { path = decodeURIComponent(ctx.path || ''); } catch { return ctx.throw(400, 'Invalid path'); }
    for (const [name,fields] of collections) {
      const direct = resource === name || path.includes('/' + name + ':');
      const association = resource.startsWith(name + '.') || resource.startsWith(name + '/') || path.includes('/' + name + '/');
      if (!direct && !association) continue;
      if (association || [ctx.action?.params,ctx.query,ctx.request?.body].some(v => invalidOptions(v,fields))) {
        return ctx.throw(403, {code:'VIFA_ASSOCIATION_READ_BLOCKED',message:'该角色仅允许读取授权普通字段，不允许展开关联记录。'});
      }
      break;
    }
    return next();
  };
}
module.exports = {createGuard};
