"""统一消费来源：认证绑定、累计响应幂等写入与精确计价。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import os
from zoneinfo import ZoneInfo

from fastapi import HTTPException

FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
MODEL_FIELDS = ('model_id', 'model_revision', 'model_name', 'display_name', 'protocol', 'pricing')


def model_snapshot(binding):
    return {key: binding[key] for key in MODEL_FIELDS if key in binding}


def estimate(usage, pricing):
    currency = pricing.get('currency')
    amount, unpriced, priced = Decimal(0), False, False
    with localcontext() as ctx:
        ctx.prec = 60
        for field in FIELDS:
            count = usage.get(field)
            rate = pricing.get(field.removesuffix('_tokens'))
            if count is None:
                unpriced = True
            elif count == 0:
                continue
            elif rate is None or not currency:
                unpriced = True
            else:
                amount += Decimal(count) * Decimal(str(rate)) / Decimal(1_000_000)
                priced = True
    if currency and all(usage.get(field) == 0 for field in FIELDS):
        priced = True
    return currency, amount if priced else None, unpriced


def register_binding(conn, identifier, *, principal, task_run_id, project_id=None, conversation_id=None):
    if principal.kind != 'machine':
        raise HTTPException(403, 'execution_owner_mismatch')
    conn.execute('SELECT pg_advisory_xact_lock(hashtext(?))', (identifier,))
    existing = conn.execute('SELECT * FROM model_consumption_bindings WHERE id=?', (identifier,)).fetchone()
    if existing:
        if (existing['owner_id'], existing['task_run_id'], existing['project_id'], existing['conversation_id']) != (principal.subject_id, task_run_id, project_id, conversation_id):
            raise HTTPException(403, 'consumption_owner_mismatch')
        return
    if not project_id and not conversation_id:
        raise HTTPException(422, 'consumption_scope_required')
    row = conn.execute('''SELECT * FROM task_runs WHERE id=?
        AND project_id IS NOT DISTINCT FROM ? AND conversation_id IS NOT DISTINCT FROM ? FOR UPDATE''',
        (task_run_id, project_id, conversation_id)).fetchone()
    if row is None:
        raise HTTPException(404, 'task_run_not_found')
    context = json.loads(row['runtime_context_json'])
    if principal.kind != 'machine' or context.get('dispatcher_id') != principal.subject_id:
        raise HTTPException(403, 'execution_owner_mismatch')
    if conn.execute('''SELECT 1 FROM model_consumption_bindings WHERE task_run_id=?
        AND project_id IS NOT DISTINCT FROM ? AND conversation_id IS NOT DISTINCT FROM ? AND historical''',
        (task_run_id, project_id, conversation_id)).fetchone():
        raise HTTPException(409, 'consumption_historical_boundary')
    project, conversation = row['project_id'], row['conversation_id']
    if conversation:
        owner = conn.execute('SELECT tenant_id FROM agent_conversations WHERE id=?', (conversation,)).fetchone()
    else:
        owner = conn.execute('SELECT tenant_id FROM projects WHERE id=?', (project,)).fetchone()
    if owner is None:
        raise HTTPException(409, 'consumption_scope_deleted')
    snapshot = model_snapshot(context.get('execution_binding') or {})
    if not snapshot.get('model_id'):
        raise HTTPException(409, 'consumption_model_binding_missing')
    rate = conn.execute('SELECT model_json FROM model_consumption_rates WHERE tenant_id=? AND model_id=? AND revision=?',
                        (owner['tenant_id'],snapshot['model_id'],snapshot.get('model_revision'))).fetchone()
    if rate is None:
        raise HTTPException(409, 'consumption_rate_binding_missing')
    authoritative = json.loads(rate['model_json'])
    if any(snapshot.get(key) != authoritative.get(key) for key in ('model_name','protocol')):
        raise HTTPException(409, 'consumption_model_binding_mismatch')
    snapshot = authoritative
    conn.execute('''INSERT INTO model_consumption_bindings
        (id,tenant_id,owner_id,source,task_run_id,project_id,conversation_id,model_json)
        VALUES (?,?,?,?,?,?,?,?)''', (identifier,owner['tenant_id'],principal.subject_id,row['task_type'],
                                    task_run_id,project,conversation,json.dumps(snapshot)))
    if conversation:
        # 延迟注册也需通知增量消息读取，不能等到第一条回执才看见未知状态。
        conn.execute("UPDATE agent_messages SET revision=nextval('agent_message_revision') WHERE task_run_id=? AND conversation_id=?",
                     (task_run_id, conversation))


def receipt_digest(event):
    return sha256(json.dumps(event, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def save_response(conn, identifier, event):
    digest = receipt_digest(event)
    binding = conn.execute('SELECT * FROM model_consumption_bindings WHERE id=? FOR UPDATE', (identifier,)).fetchone()
    if binding is None:
        raise HTTPException(404, 'consumption_binding_not_found')
    prior = conn.execute('SELECT * FROM model_consumption WHERE binding_id=? AND response_seq=?',
                         (identifier,event['response_seq'])).fetchone()
    if prior and prior['revision'] > event['revision']:
        return
    usage = {field: event.get('usage', {}).get(field) for field in FIELDS}
    if any(value is not None and (type(value) is not int or value < 0) for value in usage.values()):
        raise HTTPException(422, 'consumption_invalid_tokens')
    if prior:
        # 乱序回执不倒退，同一累计响应只更新已知字段，不将先前已知值清成未知。
        for field in FIELDS:
            if usage[field] is None:
                usage[field] = prior[field]
            elif prior[field] is not None and usage[field] < prior[field]:
                raise HTTPException(409, 'consumption_counter_regression')
        if prior['revision'] == event['revision']:
            if prior['receipt_sha256'] != digest:
                raise HTTPException(409, 'consumption_receipt_conflict')
            return
    snapshot = json.loads(binding['model_json'])
    currency, amount, unpriced = estimate(usage, snapshot.get('pricing') or {})
    measured_at = event.get('occurred_at')
    time_basis = event.get('time_basis', 'response')
    if not measured_at and time_basis != 'unknown_historical':
        measured_at = datetime.now(timezone.utc)
        time_basis = 'received'
    conn.execute('''INSERT INTO model_consumption
        (binding_id,response_seq,revision,occurred_at,time_basis,input_tokens,output_tokens,
         cache_read_tokens,cache_write_tokens,measurement,missing_responses,currency,amount,unpriced,receipt_sha256)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (binding_id,response_seq) DO UPDATE SET revision=EXCLUDED.revision,
        receipt_sha256=EXCLUDED.receipt_sha256,
        input_tokens=EXCLUDED.input_tokens,output_tokens=EXCLUDED.output_tokens,
        cache_read_tokens=EXCLUDED.cache_read_tokens,cache_write_tokens=EXCLUDED.cache_write_tokens,
        measurement=EXCLUDED.measurement,missing_responses=EXCLUDED.missing_responses,
        currency=EXCLUDED.currency,amount=EXCLUDED.amount,unpriced=EXCLUDED.unpriced''',
        (identifier,event['response_seq'],event['revision'],measured_at,time_basis,*usage.values(),
         event['measurement'],event.get('missing_responses',0),currency,amount,unpriced,digest))
    if binding['conversation_id']:
        conn.execute("UPDATE agent_messages SET revision=nextval('agent_message_revision') WHERE task_run_id=? AND conversation_id=?", (binding['task_run_id'],binding['conversation_id']))


def accept_response(conn, identifier, event, principal):
    binding = conn.execute('SELECT owner_id FROM model_consumption_bindings WHERE id=?', (identifier,)).fetchone()
    if binding is None or principal.kind != 'machine' or binding['owner_id'] != principal.subject_id:
        raise HTTPException(403, 'consumption_owner_mismatch')
    save_response(conn, identifier, event)


def normalize_http_usage(protocol, payload):
    raw = payload.get('usage') if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    def count(key, obj=raw):
        value = obj.get(key)
        return value if type(value) is int and value >= 0 else None
    if protocol == 'anthropic-messages':
        return dict(zip(FIELDS, (count('input_tokens'),count('output_tokens'),
                                count('cache_read_input_tokens'),count('cache_creation_input_tokens'))))
    responses = protocol == 'openai-responses'
    total = count('input_tokens' if responses else 'prompt_tokens')
    details = raw.get('input_tokens_details' if responses else 'prompt_tokens_details')
    cached = count('cached_tokens', details) if isinstance(details, dict) else None
    # OpenAI 不单列缓存写入；只有已知缓存读取才能拆出非缓存输入，reasoning 已含在输出中。
    return dict(zip(FIELDS, (total-cached if total is not None and cached is not None and total >= cached else None,
                            count('output_tokens' if responses else 'completion_tokens'),cached,0 if raw else None)))


_SUM = ','.join(f'SUM(c.{field}) AS {field}' for field in FIELDS)
_TOTAL = 'COALESCE(c.input_tokens,0)+COALESCE(c.output_tokens,0)+COALESCE(c.cache_read_tokens,0)+COALESCE(c.cache_write_tokens,0)'
_PARTIAL = "(c.input_tokens IS NULL OR c.output_tokens IS NULL OR c.cache_read_tokens IS NULL OR c.cache_write_tokens IS NULL OR c.missing_responses>0 OR c.measurement IN ('sdk_normalized','historical_summary'))"


def _aggregate(conn, where, params, group='', *, group_select=None, select_params=()):
    selection = group_select or group
    return conn.execute(f'''SELECT {selection + ',' if selection else ''} c.currency, {_SUM}, SUM({_TOTAL}) AS tokens,
        COUNT(*) AS responses, COUNT(*) FILTER (WHERE {_PARTIAL}) AS partial_responses,
        COUNT(*) FILTER (WHERE c.unpriced) AS unpriced_responses, SUM(c.amount) AS amount
        FROM model_consumption c JOIN model_consumption_bindings b ON b.id=c.binding_id
        WHERE {where} GROUP BY {group + ',' if group else ''} c.currency''', (*select_params, *params)).fetchall()


def _summary(rows):
    result = dict(tokens=0,responses=0,partial_responses=0,unpriced_responses=0,amounts=[],usage={field:None for field in FIELDS})
    for row in rows:
        for field in ('tokens','responses','partial_responses','unpriced_responses'):
            result[field] += int(row[field] or 0)
        for field in FIELDS:
            if row[field] is not None:
                result['usage'][field] = (result['usage'][field] or 0) + int(row[field])
        if row['amount'] is not None:
            result['amounts'].append({'currency':row['currency'],'amount':str(row['amount'])})
    return result


def message_consumption(conn, task_run_ids, *, conversation_id):
    if not task_run_ids:
        return {}
    rows = _aggregate(conn, 'b.conversation_id=? AND b.task_run_id = ANY(?)',
                      (conversation_id,list(task_run_ids)), 'b.task_run_id')
    grouped = {}
    for row in rows:
        grouped.setdefault(row['task_run_id'], []).append(row)
    result = {key:_summary(items) for key,items in grouped.items()}
    unobserved = conn.execute('''SELECT b.task_run_id, COUNT(*) AS count
        FROM model_consumption_bindings b WHERE b.conversation_id=? AND b.task_run_id = ANY(?)
        AND NOT EXISTS (SELECT 1 FROM model_consumption c WHERE c.binding_id=b.id)
        GROUP BY b.task_run_id''', (conversation_id,list(task_run_ids))).fetchall()
    for row in unobserved:
        summary = result.setdefault(row['task_run_id'], {})
        # 绑定尚无回执不等于已经发出了请求；单独暴露，不伪造缺失响应数或零小计。
        summary['unobserved_bindings'] = int(row['count'])
    return result


def project_consumption(conn, projects):
    """只聚合调用方已授权的项目集合，并校验绑定的租户身份；不追溯消息上下文。"""
    if not projects:
        return {}
    identifiers = list(projects)
    tenants = [projects[key] for key in identifiers]
    scope = '''EXISTS (SELECT 1 FROM unnest(?::text[], ?::text[]) AS allowed(id, tenant_id)
        WHERE allowed.id=b.project_id AND allowed.tenant_id=b.tenant_id)'''
    rows = _aggregate(conn, scope, (identifiers, tenants), 'b.project_id')
    grouped = {}
    for row in rows:
        grouped.setdefault(row['project_id'], []).append(row)
    result = {key: {**_summary(grouped.get(key, [])), 'unobserved_bindings': 0} for key in identifiers}
    unobserved = conn.execute(f'''SELECT b.project_id, COUNT(*) AS count
        FROM model_consumption_bindings b WHERE {scope}
        AND NOT EXISTS (SELECT 1 FROM model_consumption c WHERE c.binding_id=b.id)
        GROUP BY b.project_id''', (identifiers, tenants)).fetchall()
    for row in unobserved:
        result[row['project_id']]['unobserved_bindings'] = int(row['count'])
    return result


def overview_consumption(conn, principal, *, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(os.environ.get('CAIRN_BUSINESS_TIMEZONE', 'UTC'))
    today = now.astimezone(zone).replace(hour=0,minute=0,second=0,microsecond=0)
    start = today-timedelta(days=13)
    end = today+timedelta(days=1)
    scope, params = ('TRUE', ()) if principal.is_super_admin else ('b.tenant_id=?', (principal.tenant_id,))
    total = _summary(_aggregate(conn, scope, params))
    unobserved = conn.execute(f'''SELECT COUNT(*) AS count FROM model_consumption_bindings b
        WHERE {scope} AND NOT EXISTS (SELECT 1 FROM model_consumption c WHERE c.binding_id=b.id)''', params).fetchone()
    daily = _aggregate(
        conn, scope+' AND c.occurred_at>=? AND c.occurred_at<?', (*params,start,end),
        'business_day', group_select='(c.occurred_at AT TIME ZONE ?)::date AS business_day',
        select_params=(str(zone),),
    )
    grouped = {}
    for row in daily:
        grouped.setdefault(str(row['business_day']), []).append(row)
    migration = conn.execute('SELECT applied_at FROM schema_migrations WHERE version=83').fetchone()
    enabled_at = migration['applied_at'] if migration else now
    enabled = enabled_at if isinstance(enabled_at, datetime) else datetime.fromisoformat(enabled_at.replace('Z','+00:00'))
    if enabled.tzinfo is None:
        enabled = enabled.replace(tzinfo=timezone.utc)
    days = []
    for index in range(14):
        day = start+timedelta(days=index)
        summary = _summary(grouped.get(day.date().isoformat(), []))
        coverage = 'unavailable' if day+timedelta(days=1) <= enabled else 'partial'
        days.append({'date':day.date().isoformat(), 'coverage':coverage, **summary})
    undated = _summary(_aggregate(conn, scope+' AND c.occurred_at IS NULL', params))
    models = _aggregate(conn, scope+' AND c.occurred_at>=? AND c.occurred_at<?', (*params,start,end),
                        "b.model_json,b.source")
    grouped_models = {}
    for row in models:
        key=(row['model_json'],row['source'])
        grouped_models.setdefault(key,[]).append(row)
    return {'scope':'global' if principal.is_super_admin else 'tenant','timezone':str(zone),
            'enabled_at':enabled.isoformat(),'generated_at':now.isoformat(),
            'coverage':'recorded_only','unobserved_bindings':int(unobserved['count']),'undated':undated,
            'start':start.astimezone(timezone.utc).isoformat(),'end':end.astimezone(timezone.utc).isoformat(),
            'total':total,'today':days[-1], 'days':days,
            'models':sorted([{'model':json.loads(key[0]),'source':key[1],**_summary(items)}
                             for key,items in grouped_models.items()],key=lambda item:item['tokens'],reverse=True)}
