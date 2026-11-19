"""Read saved official results without constructing a pool or calling a provider."""
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

CHANNEL = 'official_antigravity_cli_isolated'
KEY = re.compile(r'[0-9a-f]{64}')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def classify(status, response, note, blocked):
    # A read-only conversation restriction is independent of the provider outcome.
    if status == 'completed' and isinstance(response, str) and response.strip():
        return 'succeeded', 'not_required_for_this_call'
    if status in ('dispatching', 'queued', 'running'):
        return 'processing', 'unknown'
    evidence = (note or '') + ' ' + (blocked or '')
    validation = bool(re.search(r'VALIDATION_REQUIRED|verify your account|要求.*核验|要求验证|仍需核验', evidence, re.I))
    if validation:
        page = 'page_error_500' if re.search(r'\b500\b', blocked or '') else 'required'
        return 'verification_required', page
    return ('failed' if status == 'failed' else 'unconfirmed'), 'unknown'


def read_status(database, runtime=None):
    result = {'schema_version': 1, 'available': False, 'read_at': time.time(),
              'channel': CHANNEL, 'production_send_available': False,
              'authorization_source': 'not_connected', 'accounts': [], 'conversations': {}}
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            # No title, label, error text, or model name is used to guess the channel.
            official = {}
            for row in db.execute('SELECT id,native_json,blocked FROM conversations'):
                try:
                    native = json.loads(row['native_json'] or '{}')
                except (ValueError, TypeError):
                    continue
                if isinstance(native, dict) and (native.get('official_cli_trial') is True or native.get('channel') == 'official_antigravity_cli'):
                    official[row['id']] = (row['blocked'], native.get('model'), native.get('channel') or CHANNEL)
            controls = {r['account_key']: dict(r) for r in db.execute('SELECT account_key,gemini_enabled FROM account_preferences')}
            latest = {}
            latest_probes = {}
            latest_successes = {}
            for row in db.execute('''SELECT r.request_id,r.account_key,r.status,r.created,r.finished,r.response,r.note,
                t.conversation_id,m.first_text_ms,m.total_ms,am.model FROM requests r
                JOIN conversation_turns t USING(request_id)
                LEFT JOIN request_metrics m USING(request_id)
                LEFT JOIN conversation_attempt_models am USING(request_id)
                ORDER BY r.created DESC,r.request_id DESC'''):
                if row['conversation_id'] not in official or row['account_key'] in latest:
                    continue
                if not KEY.fullmatch(row['account_key'] or '') or number(row['created']) is None:
                    continue
                blocked, native_model, channel = official[row['conversation_id']]
                outcome, verification = classify(row['status'], row['response'], row['note'], blocked)
                model = row['model'] or native_model
                model = model if isinstance(model, str) and re.fullmatch(r'[a-zA-Z0-9._/-]{1,128}', model) else None
                latest[row['account_key']] = {'request_id': row['request_id'], 'conversation_id': row['conversation_id'],
                    'outcome': outcome, 'verification': verification, 'created': row['created'],
                    'finished': number(row['finished']), 'first_text_ms': number(row['first_text_ms']),
                    'total_ms': number(row['total_ms']), 'model': model, 'channel': channel}
            # Deleting the conversation list does not remove the separate official
            # execution ledger. Keep its proof, without returning a deleted link.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_jobs'").fetchone():
                probe='j.readiness_probe' if any(row['name']=='readiness_probe' for row in db.execute('PRAGMA table_info(official_jobs)')) else '0'
                for row in db.execute(f"""SELECT r.request_id,r.account_key,r.status,r.created,r.finished,r.response,r.note,j.reason,{probe} AS readiness_probe,
                    j.model,t.conversation_id,c.blocked,m.first_text_ms,m.total_ms
                    FROM official_jobs j JOIN requests r USING(request_id)
                    LEFT JOIN conversation_turns t USING(request_id)
                    LEFT JOIN conversations c ON c.id=t.conversation_id
                    LEFT JOIN request_metrics m USING(request_id)
                    ORDER BY r.created DESC,r.request_id DESC"""):
                    key=row['account_key'];created=number(row['created'])
                    if not KEY.fullmatch(key or '') or created is None:continue
                    if row['readiness_probe'] and key not in latest_probes:
                        latest_probes[key]={'request_id':row['request_id'],'status':row['status'],'reason':row['reason'],
                                           'finished':number(row['finished']),'total_ms':number(row['total_ms'])}
                    finished=number(row['finished'])
                    if (row['status']=='completed' and isinstance(row['response'],str) and row['response'].strip()
                            and finished is not None and finished>(latest_successes.get(key) or {}).get('finished',0)):
                        latest_successes[key]={'request_id':row['request_id'],'finished':finished,'total_ms':number(row['total_ms'])}
                    previous=latest.get(key)
                    event_at=finished if row['status']=='completed' and finished is not None else created
                    previous_at=((previous.get('finished') or previous['created']) if previous.get('outcome')=='succeeded'
                                 else previous['created']) if previous else 0
                    if previous and (previous_at>event_at or
                                     previous_at==event_at and previous['request_id']!=row['request_id']):continue
                    outcome,verification=classify(row['status'],row['response'],row['note'],row['blocked'])
                    model=row['model']
                    latest[key]={'request_id':row['request_id'],'conversation_id':row['conversation_id'],
                        'outcome':outcome,'verification':verification,'created':created,
                        'finished':number(row['finished']),'first_text_ms':number(row['first_text_ms']),
                        'total_ms':number(row['total_ms']),
                        'model':model if isinstance(model,str) and re.fullmatch(r'[a-zA-Z0-9._/-]{1,128}',model) else None,
                        'channel':'official_antigravity_cli','readiness_probe':bool(row['readiness_probe'])}
            runtime_accounts = (runtime or {}).get('accounts', {})
            for key in sorted(set(controls) | set(latest) | set(runtime_accounts)):
                if not KEY.fullmatch(key):
                    continue
                control = controls.get(key, {})
                pause_source = 'manual_preference' if control.get('gemini_enabled') == 0 else None
                paused = pause_source is not None
                call = latest.get(key)
                result['accounts'].append({'account_key': key, 'paused': paused, 'pause_source': pause_source, 'latest_call': call,
                    'latest_probe': latest_probes.get(key),
                    'latest_success': latest_successes.get(key),
                    'block_reason': runtime_accounts.get(key, {}).get('block_reason'),
                    'authorization': runtime_accounts.get(key, {}).get('authorization', 'not_connected'),
                    'dispatch_eligible': runtime_accounts.get(key, {}).get('dispatch_eligible', False),
                    'advanced_text_models': runtime_accounts.get(key, {}).get('advanced_text_models', []),
                    'advanced_quota_restriction': runtime_accounts.get(key, {}).get('advanced_quota_restriction'),
                    'busy': runtime_accounts.get(key, {}).get('busy', False),
                    'restriction': runtime_accounts.get(key, {}).get('restriction'), 'source': 'saved_official_requests'})
            result['conversations'] = {ident: value[2] for ident,value in official.items()}
            if runtime:
                result.update(channel='official_antigravity_cli', production_send_available=runtime.get('available') is True,
                              authorization_source='saved_verified_identity',
                              dispatch={k:runtime.get(k) for k in ('queued','active','selection','max_concurrency','retry','failover','model','shared_dispatch','account_dispatch','concurrency_scope','per_account_concurrency','advanced_pool')})
            result['available'] = True
    except (sqlite3.Error, OSError):
        # Do not forward DB errors, private native metadata, response bodies or URLs.
        pass
    return result
