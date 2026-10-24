"""Reported translation usage, not ChatGPT billing; never store prompts or credentials."""
from datetime import datetime, timedelta, timezone
import re
import secrets
import time

MODEL = 'gpt-6-luna'
PRICE_DATE = '2026-10-25'
PRICE_URL = 'https://developers.openai.com/api/docs/models/gpt-6-luna'
# USD per million tokens; integer picodollars keep every recorded charge exact.
RATES = {'input': 0.10, 'cached_input': 0.01, 'cache_write': 0.125, 'output': 0.50}
COUNTS = ('input_tokens', 'cached_tokens', 'cache_write_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')


def init_db(connect):
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS reader_translation_usage (
            request_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, episode_id TEXT NOT NULL,
            source_hash TEXT NOT NULL, work_title TEXT NOT NULL, episode_title TEXT NOT NULL,
            model TEXT NOT NULL, service_tier TEXT NOT NULL DEFAULT '', prefetch INTEGER NOT NULL,
            started_at INTEGER NOT NULL, finished_at INTEGER, state TEXT NOT NULL DEFAULT 'running',
            response_id TEXT NOT NULL DEFAULT '', input_tokens INTEGER, cached_tokens INTEGER,
            cache_write_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
            cost_pico_usd INTEGER, price_date TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS reader_usage_started ON reader_translation_usage(started_at)')
        db.execute('CREATE INDEX IF NOT EXISTS reader_usage_source ON reader_translation_usage(work_id,episode_id,source_hash,model)')


def begin(connect, episode, model, prefetch):
    request_id = secrets.token_hex(16)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        work = db.execute('SELECT title FROM reader_works WHERE work_id=?', (episode['work_id'],)).fetchone()
        db.execute('''INSERT INTO reader_translation_usage
            (request_id,work_id,episode_id,source_hash,work_title,episode_title,model,prefetch,started_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (request_id, episode['work_id'] if work else '', episode['episode_id'] if work else '',
            episode['source_hash'] if work else '', (work['title'] if work else '삭제된 작품')[:300],
            episode['title'][:300] if work else '', model, int(prefetch), int(time.time()*1000)))
    return request_id


def measurement(response):
    result = {key: None for key in COUNTS}
    result.update(cost_pico_usd=None, price_date='', note='사용량 미보고')
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return result
    def valid(value):
        return type(value) is int and 0 <= value <= 1_000_000_000
    if not all(valid(usage.get(key)) for key in ('input_tokens', 'output_tokens', 'total_tokens')):
        result['note'] = '사용량 형식 확인 불가'
        return result
    if usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']:
        result['note'] = '사용량 합계 불일치'
        return result
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        result[key] = usage[key]
    inputs = usage.get('input_tokens_details') or {}
    outputs = usage.get('output_tokens_details') or {}
    for key in ('cached_tokens', 'cache_write_tokens'):
        value = inputs.get(key) if isinstance(inputs, dict) else None
        result[key] = value if valid(value) else None
    reasoning = outputs.get('reasoning_tokens') if isinstance(outputs, dict) else None
    result['reasoning_tokens'] = reasoning if valid(reasoning) and reasoning <= usage['output_tokens'] else None
    cached, written = result['cached_tokens'], result['cache_write_tokens']
    if cached is None or written is None:
        result['note'] = '캐시 세부 사용량 미보고 · 금액 미확인'
        return result
    ordinary = result['input_tokens'] - cached - written
    if ordinary < 0:
        result['note'] = '캐시 사용량 불일치 · 금액 미확인'
        return result
    if not re.fullmatch(r'gpt-6-luna(?:-\d{4}-\d{2}-\d{2})?', str(response.get('model', ''))):
        result['note'] = '모델 요금 미등록'
        return result
    # The transport reports the actual response tier; never guess from a requested tier.
    tier = response.get('service_tier')
    factor = {'default': (1, 1), 'priority': (2, 1), 'flex': (1, 2)}.get(tier) if isinstance(tier, str) else None
    if factor is None:
        result['note'] = '응답 처리 등급 미확인'
        return result
    long_input = result['input_tokens'] > 272_000
    input_cost = (ordinary*100_000 + cached*10_000 + written*125_000) * (2 if long_input else 1)
    output_cost = result['output_tokens'] * (750_000 if long_input else 500_000)
    result.update(cost_pico_usd=(input_cost+output_cost)*factor[0]//factor[1], price_date=PRICE_DATE, note='')
    return result


def finish(connect, request_id, response, state):
    data = measurement(response)
    def label(value):
        return value[:128] if isinstance(value, str) else ''
    fields = (*COUNTS, 'cost_pico_usd', 'price_date', 'note')
    with connect() as db:
        db.execute('UPDATE reader_translation_usage SET '+','.join(key+'=?' for key in fields)+''',
            model=CASE WHEN ?!='' THEN ? ELSE model END,service_tier=?,response_id=?,state=?,finished_at=?
            WHERE request_id=? AND state='running' ''',
            (*[data[key] for key in fields], label(response.get('model')), label(response.get('model')),
             label(response.get('service_tier')), label(response.get('id')), state, int(time.time()*1000), request_id))


def interrupt(connect):
    with connect() as db:
        db.execute("UPDATE reader_translation_usage SET state='interrupted',finished_at=?,note='서버 재시작 · 사용량 미확인' WHERE state='running'",
                   (int(time.time()*1000),))


def unlink_work(db, work_id):
    # Spending is historical; remove the deleted work's identifying information, not its totals.
    db.execute("""UPDATE reader_translation_usage SET work_id='',episode_id='',source_hash='',
        work_title='삭제된 작품',episode_title='' WHERE work_id=?""", (work_id,))


def snapshot(db):
    today = int(datetime.now(timezone(timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    sums = ','.join('COALESCE(sum('+key+'),0) AS '+key for key in COUNTS)
    aggregates = """count(*) AS requests,sum(input_tokens IS NOT NULL) AS reported,
        sum(cost_pico_usd IS NOT NULL) AS priced,sum(state='running') AS running,
        sum(state IN ('failed','interrupted')) AS failed,COALESCE(sum(cost_pico_usd),0) AS cost_pico_usd,"""+sums
    def totals(row):
        item = dict(row)
        for key in ('reported','priced','running','failed'):
            item[key] = item[key] or 0
        item['unreported'] = item['requests'] - item['reported']
        item['unpriced'] = item['requests'] - item['priced']
        item['cost_usd'] = item.pop('cost_pico_usd')/1_000_000_000_000 if item['priced'] or not item['requests'] else None
        if not item['reported'] and item['requests']:
            item['total_tokens'] = None
        return item
    total = totals(db.execute('SELECT '+aggregates+' FROM reader_translation_usage').fetchone())
    daily = totals(db.execute('SELECT '+aggregates+' FROM reader_translation_usage WHERE started_at>=?', (today,)).fetchone())
    works = [totals(row) for row in db.execute('SELECT work_id,max(work_title) AS title,'+aggregates+
        ' FROM reader_translation_usage GROUP BY work_id ORDER BY max(started_at) DESC')]
    recent = []
    for row in db.execute('''SELECT work_title,episode_title,model,service_tier,prefetch,started_at,finished_at,state,
        input_tokens,cached_tokens,cache_write_tokens,output_tokens,reasoning_tokens,total_tokens,cost_pico_usd,price_date,note
        FROM reader_translation_usage ORDER BY started_at DESC,rowid DESC LIMIT 20'''):
        item = dict(row)
        cost = item.pop('cost_pico_usd')
        item['cost_usd'] = cost/1_000_000_000_000 if cost is not None else None
        recent.append(item)
    untracked = db.execute('''SELECT count(*) FROM reader_translations t WHERE t.state='ready' AND NOT EXISTS (
        SELECT 1 FROM reader_translation_usage u WHERE u.work_id=t.work_id AND u.episode_id=t.episode_id
        AND u.source_hash=t.source_hash AND u.model=t.model)''').fetchone()[0]
    return {'total': total, 'today': daily, 'works': works, 'recent': recent, 'untracked_cached': untracked,
            'currency': 'USD', 'timezone': 'Asia/Seoul', 'model': MODEL, 'rates': RATES,
            'price_date': PRICE_DATE, 'price_url': PRICE_URL}
