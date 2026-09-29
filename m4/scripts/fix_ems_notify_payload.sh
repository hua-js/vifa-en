#!/usr/bin/env bash
# Run on the database host. Changes only the reviewed t_model notification query.
set -euo pipefail
umask 077
container="${1:-nbdb1624-holodb-1}"

db() {
  docker exec -i "$container" sh -c '
    exec psql -X -v ON_ERROR_STOP=1 -P pager=off \
      -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" "$@"
  ' sh "$@"
}

backup=$(mktemp "${PWD}/notify_load_kw.before.XXXXXX.sql")
db -qAt > "$backup" <<'SQL'
BEGIN READ ONLY;
SELECT pg_get_functiondef('public.notify_load_kw()'::regprocedure);
COMMIT;
SQL
test -s "$backup"
printf '原函数备份：%s\n' "$backup"

db <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
DO $fix$
DECLARE
    original text := pg_get_functiondef('public.notify_load_kw()'::regprocedure);
    old_query text := '(SELECT json_agg(t) FROM t_model t)';
    new_query text := $query$(SELECT json_agg(t) FROM public.t_model t WHERE t.repeat IS DISTINCT FROM '已过期')$query$;
    revised text;
    payload_bytes integer;
BEGIN
    IF strpos(original, new_query) > 0 AND strpos(original, old_query) = 0 THEN
        RAISE NOTICE '过滤已存在，无需重复修改';
    ELSE
        IF (length(original) - length(replace(original, old_query, ''))) <> length(old_query) THEN
            RAISE EXCEPTION '函数定义与已审阅版本不匹配，未修改';
        END IF;
        revised := replace(original, old_query, new_query);
        EXECUTE revised;
        IF pg_get_functiondef('public.notify_load_kw()'::regprocedure) <> revised THEN
            RAISE EXCEPTION '函数回读与预期不一致，回滚';
        END IF;
    END IF;
    SELECT octet_length(json_build_object('models', json_agg(t))::text)
      INTO payload_bytes
      FROM public.t_model t WHERE t.repeat IS DISTINCT FROM '已过期';
    IF payload_bytes >= 8000 THEN
        RAISE EXCEPTION '过滤后的通知仍过大（% 字节），回滚', payload_bytes;
    END IF;
    RAISE NOTICE '函数已核对，当前有效计划通知：% 字节', payload_bytes;
END;
$fix$;
COMMIT;
SQL
printf '修复已提交。未改动计划记录，未主动发送通知。备份：%s\n' "$backup"
