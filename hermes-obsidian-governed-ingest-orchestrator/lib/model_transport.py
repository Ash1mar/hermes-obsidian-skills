"""Durable admission and telemetry at the fixed model HTTP boundary."""
from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import json
from pathlib import Path
import os
import random
import re
import secrets
import sqlite3
import time

import httpx

from hermes_source_units.source_units import _json_bytes, _write_atomic
from hermes_source_units.validation import fingerprint


CONTRACT = 'fixed-model-http/v1'
MAX_RETRIES = 2
MAX_WAIT = 120
CIRCUIT_FAILURE_LIMIT = 3
OVERLOAD = {'server_is_overloaded', 'overloaded_error', 'rate_limit_exceeded', 'rate_limit_error'}
HEADERS = {'request-id', 'x-request-id', 'openai-request-id', 'x-response-id', 'retry-after',
           'x-ratelimit-limit-requests', 'x-ratelimit-limit-tokens', 'x-ratelimit-remaining-requests',
           'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens'}
ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')


def policy(profile='shared'):
    if profile not in ('shared', 'paced', 'concentrated'):
        raise ValueError('unknown fixed model admission profile')
    return {'profile': profile, 'min_interval': 0 if profile == 'concentrated' else 1,
            'max_inflight': 5 if profile == 'paced' else 10, 'overload_inflight': 5,
            'recover_after_successes': 5, 'max_wait_seconds': MAX_WAIT, 'max_retries': MAX_RETRIES}


def transport_context(context, prepared):
    from semantic_output import ACTIVE

    active = ACTIVE.get()
    guard = prepared.get('live_control_check') or prepared.get('fresh_control_check')
    if not guard or (not active and not context.get('reservation')):
        return None
    if context.get('short_evidence_root'):
        root = Path(context['short_evidence_root'])
        reservation = context['reservation']
    else:
        path, journal, index = active
        root = path.parent.parent
        reservation = journal['attempts'][index]['attempt_id']
    fresh = prepared.get('fresh_control_check') if prepared.get('live_control_check') else None
    return {**context, 'audit_root': root, 'reservation': reservation, 'control_check': guard, 'fresh_check': fresh,
            'wire_sha256': fingerprint(prepared['wire'])}


def retry_after(value, now):
    if not value:
        return None
    try:
        delay = float(value) if re.fullmatch(r'\d+(?:\.\d+)?', value) else parsedate_to_datetime(value).timestamp() - now
        return max(0, delay)
    except (ValueError, OverflowError, TypeError):
        return None


def clean_id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def usage_of(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    elif hasattr(value, '__dict__'):
        value = vars(value)
    return {k: v for k, v in (value or {}).items()
            if k in ('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens')
            and type(v) is int} if isinstance(value, dict) else {}


class AdmissionStopped(RuntimeError):
    pass


class TransportCircuitOpen(AdmissionStopped):
    code = 'TRANSPORT_CIRCUIT_OPEN'

    def __init__(self, circuit, *, http_key=None, prior_send_count=0, report=None):
        self.circuit, self.http_key, self.report = circuit, http_key, report
        self.route_id = circuit['route_id']
        self.prior_send_count = prior_send_count
        self.send_started = bool(prior_send_count)
        super().__init__('Model transport circuit is open for route ' + self.route_id)


def journal_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / 'fixed-model-http.sqlite3'


def circuit_status(scope_key, *, transport_db=None):
    """Read the durable circuit associated with a previously observed route."""
    path = Path(transport_db) if transport_db is not None else journal_path()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='transport_circuit_scopes'").fetchone():
            return None
        route = db.execute('SELECT route FROM transport_circuit_scopes WHERE scope=?', (scope_key,)).fetchone()
        return Journal.circuit(db, route['route']) if route else None


def network_send_count(http_key, *, transport_db=None):
    """Count exact physical admissions, with a missing journal leaving the count unknown."""
    path = Path(transport_db) if transport_db is not None else journal_path()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        return db.execute('SELECT count(*) FROM sends WHERE id=?', (http_key,)).fetchone()[0]


def retire_circuit_probe(scope_key, *, route_id, trip_id, operation_id, request_id, transport_db=None):
    """Record retirement of an exact in-flight probe after its native owner has drained."""
    path = Path(transport_db) if transport_db is not None else journal_path()
    journal = Journal(path)
    with journal.transaction() as db:
        scope = db.execute('SELECT route FROM transport_circuit_scopes WHERE scope=?', (scope_key,)).fetchone()
        probe = db.execute('SELECT * FROM transport_circuit_probes WHERE scope=? AND operation_id=?',
                           (scope_key, operation_id)).fetchone()
        if (scope is None or scope['route'] != route_id or probe is None
                or probe['trip'] != trip_id or probe['request_id'] != request_id):
            raise AdmissionStopped('drained probe retirement differs from its original transport binding')
        current = journal.circuit(db, route_id)
        replacement = fingerprint([trip_id, probe['send_key'], 'retired'])
        if current['trip_id'] == replacement and current.get('probe_http_key') is None:
            return current
        if current['trip_id'] != trip_id or current.get('probe_http_key') != probe['send_key']:
            raise AdmissionStopped('drained probe retirement differs from its original transport binding')
        journal.open_circuit(db, route_id, replacement)
        return journal.circuit(db, route_id)


class Journal:
    """One host-local transaction orders all workers on a credential route."""

    def __init__(self, path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?, ?)', ('salt', secrets.token_hex(32)))
            self.salt = bytes.fromhex(db.execute("SELECT value FROM settings WHERE name='salt'").fetchone()[0])
            db.execute('CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, until REAL, cap INTEGER, streak INTEGER, level INTEGER)')
            db.execute('''CREATE TABLE IF NOT EXISTS sends (
                id TEXT, attempt INTEGER, route TEXT, sent REAL, expires REAL, ended REAL, retry INTEGER,
                evidence TEXT, PRIMARY KEY (id, attempt))''')
            db.execute('CREATE INDEX IF NOT EXISTS sends_route_time ON sends(route, sent)')
            db.execute('CREATE TABLE IF NOT EXISTS transport_circuit_scopes (scope TEXT PRIMARY KEY, route TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS transport_circuits (
                route TEXT PRIMARY KEY, failures INTEGER NOT NULL, trip TEXT, probe_key TEXT, generation TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS transport_circuit_probes (
                send_key TEXT PRIMARY KEY, scope TEXT NOT NULL, operation_id TEXT NOT NULL,
                request_id TEXT NOT NULL, trip TEXT NOT NULL,
                UNIQUE(scope, operation_id), UNIQUE(scope, request_id))''')

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db

    def route(self, request, model):
        endpoint = f'{request.url.scheme}://{request.url.host}:{request.url.port or ""}{request.url.path}'
        account = next((request.headers[k] for k in ('chatgpt-account-id', 'authorization', 'api-key', 'x-api-key')
                        if request.headers.get(k)), None)
        def opaque(value):
            return hmac.digest(self.salt, value.encode(), 'sha256').hex()[:24]
        return {'endpoint': opaque(endpoint), 'account': opaque(account) if account else None, 'model': model}

    def history(self, key):
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM sends WHERE id=? ORDER BY attempt', (key,))]

    @staticmethod
    def circuit(db, route):
        row = db.execute('SELECT * FROM transport_circuits WHERE route=?', (route,)).fetchone()
        trip = row['trip'] if row else None
        state = {'route_id': route, 'state': 'open' if trip else 'closed', 'trip_id': trip,
                 'consecutive_failures': row['failures'] if row else 0, 'probe_state': None,
                 'generation': row['generation'] if row else None}
        if trip and row['probe_key']:
            probe = db.execute('SELECT * FROM transport_circuit_probes WHERE send_key=?', (row['probe_key'],)).fetchone()
            state.update(probe_state='started', probe_http_key=row['probe_key'], probe_scope=probe['scope'],
                         probe_operation_id=probe['operation_id'], probe_request_id=probe['request_id'])
        return state

    @staticmethod
    def open_circuit(db, route, trip):
        db.execute('INSERT OR REPLACE INTO transport_circuits VALUES (?, ?, ?, NULL, ?)',
                   (route, CIRCUIT_FAILURE_LIMIT, trip, trip))

    def circuit_permission(self, db, scope, route, key, probe, prior):
        db.execute('INSERT OR IGNORE INTO transport_circuit_scopes VALUES (?, ?)', (scope, route))
        bound = db.execute('SELECT route FROM transport_circuit_scopes WHERE scope=?', (scope,)).fetchone()
        if bound['route'] != route:
            raise AdmissionStopped('the configured transport route changed for this workflow')
        state = self.circuit(db, route)
        if state['state'] == 'closed':
            return state, probe is None
        if probe is None:
            # A physical retry already granted to the same logical reservation may drain.
            return state, bool(prior and not json.loads(prior['evidence']).get('circuit_probe'))
        if probe['route_id'] != route or probe['trip_id'] != state['trip_id']:
            return state, False
        used = db.execute('''SELECT send_key FROM transport_circuit_probes
            WHERE scope=? AND (operation_id=? OR request_id=?)''', (scope, probe['operation_id'], probe['request_id'])).fetchall()
        if state.get('probe_http_key'):
            return state, (state['probe_http_key'] == key and bool(prior) and len(used) == 1
                           and json.loads(prior['evidence']).get('circuit_probe') == probe)
        return state, not used and prior is None

    def admit(self, key, route, identity, settings, now, queued, *, circuit_scope=None, probe=None, persist_attempt=None):
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM sends WHERE id=? ORDER BY attempt DESC LIMIT 1', (key,)).fetchone()
            if prior and (prior['ended'] is None or not prior['retry'] or prior['attempt'] > settings['max_retries']):
                raise AdmissionStopped('fixed HTTP reservation is spent or has unknown completion')
            if prior and (prior['route'] != route
                          or json.loads(prior['evidence'])['http_request_sha256'] != identity['http_request_sha256']):
                raise AdmissionStopped('physical retry changed its credential route or request bytes')
            circuit = None
            if circuit_scope:
                circuit, permitted = self.circuit_permission(db, circuit_scope, route, key, probe, prior)
                if not permitted:
                    return TransportCircuitOpen(circuit, http_key=key, prior_send_count=prior['attempt'] if prior else 0)
            waited = (json.loads(prior['evidence'])['cumulative_wait_seconds'] if prior else 0) + max(0, now - queued)
            if waited > settings['max_wait_seconds']:
                raise AdmissionStopped('fixed HTTP cumulative wait budget exhausted')
            state = db.execute('SELECT * FROM routes WHERE id=?', (route,)).fetchone()
            last, rate10, rate60, active = db.execute(
                '''SELECT MAX(sent), COALESCE(SUM(sent>? ),0), COALESCE(SUM(sent>? ),0),
                          COALESCE(SUM(ended IS NULL AND expires>?),0)
                   FROM sends WHERE route=? AND (sent>? OR (ended IS NULL AND expires>?))''',
                (now-10, now-60, now, route, now-60, now)).fetchone()
            cap = min(settings['max_inflight'], state['cap']) if state else settings['max_inflight']
            until = max((last or 0) + settings['min_interval'], state['until'] if state else 0)
            if until > now or active >= cap:
                return min(0.5, until - now) if until > now else 0.5
            attempt = prior['attempt'] + 1 if prior else 1
            row = {**identity, 'route_id': route, 'attempt': attempt, 'queued_at': queued, 'sent_at': now,
                   'cumulative_wait_seconds': waited, 'inflight_at_send': active + 1,
                   'sent_previous_10_seconds': rate10, 'sent_previous_60_seconds': rate60,
                   'http_status': None, 'response_headers': {}, 'request_id': None, 'response_id': None,
                   'first_response_at': None, 'first_event_at': None, 'partial_output': None, 'usage': None,
                   'sdk_internal_retries': 0, 'sdk_retries_blocked': 0, 'disposition': 'pending'}
            if circuit is not None:
                row.update(circuit=circuit, circuit_scope=circuit_scope)
                if probe:
                    row['circuit_probe'] = probe
                    if not prior:
                        db.execute('INSERT INTO transport_circuit_probes VALUES (?, ?, ?, ?, ?)',
                                   (key, circuit_scope, probe['operation_id'], probe['request_id'], probe['trip_id']))
                        db.execute('UPDATE transport_circuits SET probe_key=? WHERE route=?', (key, route))
            if not prior and persist_attempt is not None:
                persist_attempt()
            db.execute('INSERT INTO sends VALUES (?, ?, ?, ?, ?, NULL, 0, ?)',
                       (key, attempt, route, now, now+600, json.dumps(row)))
            return row

    def finish(self, key, row, settings, *, overload, retry, success, circuit_failure=None):
        now = time.time()
        route = row['route_id']
        with self.transaction() as db:
            state = db.execute('SELECT * FROM routes WHERE id=?', (route,)).fetchone()
            until, cap, streak, level = tuple(state)[1:] if state else (0, 10, 0, 0)
            if overload or retry:
                level = min(level + 1, 3)
                delay = retry_after(row['response_headers'].get('retry-after'), now)
                if delay is None:
                    delay = (5, 10, 20)[level - 1] * random.uniform(0.8, 1.2)
                until = max(until, now + delay)
                cap = min(cap, settings['overload_inflight'])
                streak = 0
                row['cooldown_until'] = until
                retry = retry and until - now + row['cumulative_wait_seconds'] <= settings['max_wait_seconds']
            elif success:
                streak += 1
                if streak >= settings['recover_after_successes'] and now >= until:
                    cap, streak, level = min(10, cap + 1), 0, max(0, level - 1)
            else:
                streak = 0
            if retry and row['attempt'] > settings['max_retries']:
                retry = False
            row.update(ended_at=now, retry_admitted=retry)
            if row['disposition'] == 'retryable_rejection' and not retry:
                row['disposition'] = 'retry_budget_exhausted'
            if row.get('circuit_scope'):
                self.finish_circuit(db, key, row, retry=retry, success=success, failure=circuit_failure)
            db.execute('INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?, ?)', (route, until, cap, streak, level))
            db.execute('UPDATE sends SET ended=?, retry=?, evidence=? WHERE id=? AND attempt=? AND ended IS NULL',
                       (now, retry, json.dumps(row), key, row['attempt']))
        return retry

    def finish_circuit(self, db, key, row, *, retry, success, failure):
        route = row['route_id']
        state = self.circuit(db, route)
        trip = state['trip_id']
        if not retry:
            if state.get('probe_http_key') == key:
                if success:
                    db.execute('UPDATE transport_circuits SET failures=0, trip=NULL, probe_key=NULL WHERE route=?', (route,))
                else:
                    self.open_circuit(db, route, fingerprint([trip, key, 'probe_failed']))
            elif (not trip and not row.get('circuit_probe')
                    and row['circuit']['generation'] == state['generation']):
                if failure:
                    count = state['consecutive_failures'] + 1
                    if count == CIRCUIT_FAILURE_LIMIT:
                        self.open_circuit(db, route, fingerprint([route, key, row['attempt'], failure]))
                    else:
                        db.execute('INSERT OR REPLACE INTO transport_circuits VALUES (?, ?, NULL, NULL, ?)',
                                   (route, count, state['generation']))
                elif state['consecutive_failures']:
                    db.execute('UPDATE transport_circuits SET failures=0 WHERE route=?', (route,))
        row['circuit'] = self.circuit(db, route)
        if failure:
            row['circuit_failure'] = failure

    def observe(self, key, row):
        with self.transaction() as db:
            db.execute('UPDATE sends SET evidence=? WHERE id=? AND attempt=? AND ended IS NULL',
                       (json.dumps(row), key, row['attempt']))


class ObservedStream(httpx.SyncByteStream):
    """Observe SSE metadata without retaining or changing provider output bytes."""

    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    def __iter__(self):
        pending = b''
        for chunk in self.stream:
            lines = (pending + chunk).split(b'\n')
            pending = lines.pop()
            if len(pending) > 262144:
                self.transport.opaque = True
                pending = b''
            for line in lines:
                if line.startswith(b'data:'):
                    try:
                        self.transport.event(json.loads(line[5:]))
                    except (ValueError, UnicodeError):
                        if line[5:].strip() != b'[DONE]':
                            self.transport.opaque = True
            yield chunk

    def close(self):
        self.stream.close()


class Transport:
    def __init__(self, context, session, model, *, input_size, output_cap):
        self.settings = policy(context.get('transport_profile', 'shared'))
        self.check = context['control_check']
        self.fresh = context.get('fresh_check')
        path = context.get('transport_db') or journal_path()
        self.journal = Journal(path)
        self.circuit_scope = context.get('circuit_scope')
        if self.circuit_scope is not None and (not isinstance(self.circuit_scope, str) or not self.circuit_scope):
            raise ValueError('transport circuit requires a stable workflow scope')
        self.probe = context.get('circuit_probe')
        self.persist_attempt = context.get('persist_attempt')
        if self.probe is not None and (
                not self.circuit_scope or not isinstance(self.probe, dict)
                or set(self.probe) != {'route_id', 'trip_id', 'operation_id', 'request_id'}
                or any(not isinstance(value, str) or not value for value in self.probe.values())
                or not isinstance(context['reservation'], dict)
                or self.probe['request_id'] != context['reservation'].get('request_sha256')):
            raise ValueError('transport probe is outside its authorized original request')
        if self.persist_attempt is not None and (not self.circuit_scope or not callable(self.persist_attempt)):
            raise ValueError('physical-attempt persistence requires the opted-in HTTP transport')
        self.root = Path(context['audit_root'])
        self.key = fingerprint([str(self.root), context['binding'], context['reservation'], context['wire_sha256']])
        self.identity = {'contract': CONTRACT, 'binding': context['binding'], 'reservation': context['reservation'],
                         'wire_sha256': context['wire_sha256'], 'phase': context['phase'], 'session_id': session,
                         'model': model, 'input_codepoints': input_size, 'output_cap_tokens': output_cap,
                         'policy': self.settings}
        self.row = None
        self.refs = []
        self.opaque = False
        self.error = {}
        self.block_reason = None
        self.circuit = None
        self.circuit_block = None
        self.accepting = True
        self.lookup_id = None
        self.lookup_client = None
        history = self.journal.history(self.key)
        for attempt in history:
            if attempt['ended'] is not None:
                self.export(json.loads(attempt['evidence']))
        self.queued = history[-1]['ended'] if history and history[-1]['ended'] else time.time()

    def attach(self, client):
        http = getattr(client, '_client', None)
        if not isinstance(http, httpx.Client) or getattr(client, 'max_retries', None) != 0:
            raise AdmissionStopped('fixed model HTTP client cannot prove SDK retry ownership')
        if self.before not in http.event_hooks['request']:
            http.event_hooks['request'].append(self.before)
            http.event_hooks['response'].append(self.response)
        self.lookup_client = client

    def before(self, request):
        if self.circuit_block is not None:
            raise self.circuit_block
        if self.lookup_id is not None:
            expected = '/v1/responses/' + self.lookup_id
            if (request.method != 'GET' or request.url.scheme != 'https' or request.url.host != 'api.openai.com'
                    or request.url.path != expected or self.row['lookup'].get('sent_at') is not None):
                raise AdmissionStopped('response lookup left its exact read-only route')
            self.check()
            if self.fresh:
                self.fresh()
            self.row['lookup']['sent_at'] = time.time()
            self.journal.observe(self.key, self.row)
            return
        if self.row is not None:
            self.row['sdk_retries_blocked'] += 1
            raise AdmissionStopped('unbudgeted SDK HTTP attempt blocked')
        route = self.journal.route(request, self.identity['model'])
        identity = {**self.identity, 'route': route, 'http_request_sha256': hashlib.sha256(request.content).hexdigest(),
                    'http_request_bytes': len(request.content)}
        while True:
            try:
                if not self.accepting:
                    raise AdmissionStopped('native HTTP owner has stopped accepting sends')
                self.check()
                if self.fresh:
                    self.fresh()
                action = self.journal.admit(self.key, fingerprint(route), identity, self.settings, time.time(), self.queued,
                                           circuit_scope=self.circuit_scope, probe=self.probe,
                                           persist_attempt=self.persist_attempt)
            except Exception as exc:
                self.block_reason = type(exc).__name__ + ': ' + str(exc)[:400]
                raise AdmissionStopped(self.block_reason) from exc
            if isinstance(action, TransportCircuitOpen):
                self.circuit = action.circuit
                self.circuit_block = action
                self.block_reason = action.code
                raise action
            if isinstance(action, dict):
                self.row = action
                self.row['lookup_supported'] = (
                    request.url.scheme == 'https' and request.url.host == 'api.openai.com'
                    and request.url.path == '/v1/responses' and request.method == 'POST'
                    and json.loads(request.content).get('store') is True)
                return
            time.sleep(action)

    def response(self, response):
        row = self.row
        if self.lookup_id is not None:
            row['lookup'].update(http_status=response.status_code, first_response_at=time.time(),
                                 request_id=clean_id(response.headers.get('x-request-id')))
            return
        row.update(first_response_at=time.time(), http_status=response.status_code,
                   response_headers={k: response.headers[k][:160] for k in HEADERS if k in response.headers},
                   partial_output=False)
        headers = row['response_headers']
        row['request_id'] = next((clean_id(headers[k]) for k in ('x-request-id', 'request-id', 'openai-request-id') if k in headers), None)
        row['response_id'] = clean_id(headers.get('x-response-id'))
        self.journal.observe(self.key, row)
        if response.headers.get('content-type', '').split(';')[0] == 'text/event-stream':
            if response.headers.get('content-encoding', 'identity').lower() == 'identity':
                response.stream = ObservedStream(response.stream, self)
            else:
                self.opaque = True

    def event(self, value):
        if not isinstance(value, dict):
            self.opaque = True
            return
        row = self.row
        before = tuple(row.get(k) for k in ('first_event_at', 'response_id', 'usage', 'partial_output', 'error_code'))
        if row['first_event_at'] is None:
            row['first_event_at'] = time.time()
        kind = value.get('type', '')
        response = value.get('response')
        if isinstance(response, dict):
            row['response_id'] = clean_id(response.get('id')) or row['response_id']
            row['usage'] = usage_of(response.get('usage')) or row['usage']
            row['partial_output'] |= bool(response.get('output'))
            if isinstance(response.get('error'), dict):
                self.error = response['error']
        if kind == 'error':
            self.error = value.get('error') if isinstance(value.get('error'), dict) else value
        if self.error:
            row['error_code'] = clean_id(self.error.get('code'))
            row['error_type'] = clean_id(self.error.get('type'))
        if isinstance(kind, str) and (kind.endswith('.delta') and bool(value.get('delta'))
                                      or kind in ('response.output_item.done', 'response.content_part.done')):
            row['partial_output'] = True
        if before != tuple(row.get(k) for k in ('first_event_at', 'response_id', 'usage', 'partial_output', 'error_code')):
            self.journal.observe(self.key, row)

    def retrieve(self, outcome):
        """Read a stored response only on the verified direct Responses route."""
        row = self.row
        if row is None:
            return None
        candidate = row['response_id'] or clean_id(outcome['error'].get('response_id'))
        if not (candidate and row['lookup_supported'] and self.lookup_client and candidate.startswith('resp_')):
            return None
        self.lookup_id = candidate
        row['lookup'] = {'response_id': candidate, 'disposition': 'unconfirmed'}
        try:
            response = self.lookup_client.responses.retrieve(candidate)
            if getattr(response, 'id', None) == candidate and getattr(response, 'status', None) == 'completed':
                row['lookup']['disposition'] = 'completed'
                return response
        except Exception as exc:
            row['lookup']['error_class'] = type(exc).__name__
        finally:
            row['lookup']['ended_at'] = time.time()
            self.lookup_id = None
        return None

    def finish(self, *, response=None, error=None, outcome=None):
        self.accepting = False
        if self.row is None:
            return False
        row = self.row
        detail = (outcome or {}).get('error', {})
        if response is not None:
            row['response_id'] = clean_id(getattr(response, 'id', None)) or row['response_id']
            row['usage'] = usage_of(getattr(response, 'usage', None)) or row['usage']
        row['error_type'] = clean_id(self.error.get('type')) or clean_id(detail.get('type'))
        row['error_code'] = clean_id(self.error.get('code')) or clean_id(detail.get('code'))
        if detail.get('response_id'):
            row['response_id'] = clean_id(detail['response_id']) or row['response_id']
        if self.opaque and row['partial_output'] is not True:
            row['partial_output'] = None
        overload = row['http_status'] == 429 or row['error_code'] in OVERLOAD or row['error_type'] in OVERLOAD
        cause = error
        while cause and getattr(cause, '__cause__', None):
            cause = cause.__cause__
        preconnect = isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        known_rejection = (row['http_status'] == 429 and row['partial_output'] is False and not row['response_id']
                           and row['error_code'] not in ('insufficient_quota', 'billing_hard_limit_reached'))
        retry = bool((known_rejection or (preconnect and row['http_status'] is None and not row['sdk_retries_blocked']))
                     and not row['response_id'] and row['partial_output'] is not True)
        success = response is not None and outcome is None
        row['disposition'] = ('completed' if success else 'retryable_rejection' if retry else
                              'completion_unknown' if (outcome or {}).get('state') != 'request_rejected'
                              else 'request_rejected')
        failure = circuit_failure(row, outcome) if self.circuit_scope else None
        retry = self.journal.finish(self.key, row, self.settings, overload=overload, retry=retry, success=success,
                                    circuit_failure=failure)
        self.circuit = row.get('circuit')
        self.export(row)
        self.queued = row['ended_at']
        self.row = None
        self.error = {}
        self.opaque = False
        return retry

    def export(self, row):
        ref = self.root / 'http-sends' / f"{self.key}-{row['attempt']}.json"
        data = _json_bytes(row)
        if ref.exists() and ref.read_bytes() != data:
            raise AdmissionStopped('fixed HTTP evidence already exists with different bytes')
        if not ref.exists():
            _write_atomic(ref, data)
        self.refs.append({'ref': ref.relative_to(self.root).as_posix(), 'sha256': hashlib.sha256(data).hexdigest()})

    def evidence(self):
        return {'contract': CONTRACT, 'policy': self.settings, 'physical_attempts': len(self.refs),
                'sends': list(self.refs), 'admission_stop': self.block_reason,
                **({'circuit': self.circuit} if self.circuit is not None else {})}


def circuit_failure(row, outcome):
    if (outcome or {}).get('state') != 'completion_unknown':
        return None
    error = outcome.get('error', {})
    if error.get('class') == 'APIConnectionError':
        return 'unknown_connection'
    if (error.get('class') == 'TimeoutError' and row['first_event_at'] is None
            and re.match(r'^Codex stream produced no parsed stream event within [\d.]+s \(TTFB threshold:',
                         error.get('message') or '')):
        return 'first_sse_event_timeout'
    return None
