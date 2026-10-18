"""Reuse genuine audited responses only against identical fresh reading authority."""
import copy
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from fixed_semantic_schema import validate, slot_results


def _request_text(user):
    """Recognize only the two emitted native framings, without searching for JSON."""
    header, separator, payload = user.partition('\n')
    if not separator:
        raise ValueError('semantic input has no supported header')
    if header == 'Semantic input v1':
        return payload
    if header.startswith('Native task '):
        identity = header[len('Native task '):]
        if identity and not any(char.isspace() for char in identity):
            return payload
    raise ValueError('semantic input has an unsupported header')


def task_content(view, task):
    task=copy.deepcopy(task)
    task.pop('task',None)
    for material in task.get('materials',[]):
        ref=material.pop('text_ref',None)
        material['text']=view.get('texts',{}).get(ref)
        heading=material.get('heading')
        headings=view.get('headings',{})
        material['heading']=headings.get(str(heading)) if isinstance(headings,dict) else headings[heading]
        qa=material.pop('qa_ref',None)
        if qa: material['qa']=view.get('qa',{}).get(qa)
    return fingerprint({'task':task,'qa_rule':view.get('qa_rule')})


def native_reply(adapter,binding,path,wire,output_schema,images,*,home=None,seen=None):
    """Authenticate raw native output, including invalid JSON, without approval."""
    adapter.worker_check(binding);w=adapter.workflow.status(binding['workflow_id'])
    permitted={binding['task_id']:None}
    for pointer in w.get('execution_recovery_history',[]):
        rp=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        if pointer.get('report_sha256') and __import__('hashlib').sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:return None
        report=_load_json(rp)
        if report['request'].get('recovery_scope') not in ('reduce-cancel/v1','source-packets/v1','source-hold/v1','source-hold/v2','source-hold/v3','source-format/v1','dependency-resolved/v1','source-result-review/v1','semantic-output/v1'):continue
        for e in report['entries']:
            if e['node']==binding['node']:permitted[e['previous_task_id']]=e
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-calls"
    seen=set() if seen is None else seen
    if path.parent!=root or str(path) in seen:return None
    seen.add(str(path));a=_load_json(path)
    if (a.get('workflow_id')!=binding['workflow_id'] or a.get('node')!=binding['node']
        or a.get('native_task_id') not in permitted or a.get('template_hash')!=binding['template_hash']
        or a.get('input_sha256')!=fingerprint(wire) or a.get('user_sha256')!=fingerprint(wire['user'])
        or a.get('schema_sha256')!=fingerprint(output_schema)
        or a.get('image_inputs',[])!=[{k:v for k,v in i.items() if k!='url'} for i in images]):return None
    previous=permitted[a['native_task_id']]
    if previous and str(a.get('native_run_id')) not in previous['ended_run_ids']:return None
    if a.get('reused_from_audit'):
        found=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,a['reused_from_audit']),wire,output_schema,images,home=home,seen=seen)
        return found if found and found[1]['response_sha256']==a['response_sha256'] else None
    raw=transcript_reply(a,wire,home=home)
    adapter.worker_check(binding)
    return (raw,a) if raw is not None else None


def transcript_reply(a,wire,*,home=None):
    """Read-only transcript proof; the caller separately checks task authority."""
    model=a.get('model',{});home=Path(home) if home else Path.home()/'.hermes'
    if model.get('origin')!='hermes_native_model' or model.get('response_sha256')!=a['response_sha256'] or not (home/'state.db').exists():return None
    from revision_disposition import native_user
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        sid=model['session_id']
        if db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):return None
        rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(sid,)))
        if any(t and t!='[]' for _,_,t in rows):return None
        users=[u for r,c,_ in rows if r=='user' and (u:=native_user(c,a))==wire['user']]
        answers=[c for r,c,_ in rows if r=='assistant' and fingerprint(c)==a['response_sha256']]
        if len(users)!=1 or len(answers)!=1:return None
    return answers[0]


def recover_reduce(adapter, binding, phase, wire, output_schema, images, decode, *, home=None, candidate_paths=None):
    """Replay an exact genuine reducer call under a fresh authorized native identity.

    This uses existing native audit/transcript evidence, not a semantic cache.
    Submission still performs all current domain/hash/coverage checks.
    """
    if phase not in ('resource-reduce', 'global-reduce', 'global-plan', 'global-detail', 'page-write'):
        return None
    adapter.worker_check(binding)
    workflow = adapter.workflow.status(binding['workflow_id'])
    adapter.workflow.assert_execution_ready(workflow)
    permitted = {binding['task_id']: None}
    for recovery in workflow.get('execution_recovery_history', []):
        report = _load_json(_vault_path(adapter.workflow.vault, recovery['report_ref']))
        if report['request'].get('recovery_scope') not in ('reduce-cancel/v1','source-packets/v1','source-hold/v1','source-hold/v2','source-hold/v3','source-format/v1','dependency-resolved/v1','source-result-review/v1','semantic-output/v1'):
            continue
        for entry in report['entries']:
            if entry['node'] == binding['node']:
                permitted[entry['previous_task_id']] = entry
    home = Path(home) if home is not None else Path.home()/'.hermes'
    if not (home/'state.db').is_file():
        return None
    vault = adapter.workflow.vault
    root = vault/'_system/ledgers/ingest-workflows'/binding['workflow_id']/'semantic-calls'
    image_inputs = [{k:v for k,v in image.items() if k != 'url'} for image in images]
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro', uri=True) as db:
        paths=sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True) if candidate_paths is None else candidate_paths
        for path in paths:
            if path.parent!=root:raise ContractError('ACCESS_DENIED','$','replay audit is outside this workflow')
            try:
                audit = _load_json(path)
                if (audit.get('phase') != phase or audit.get('workflow_id') != binding['workflow_id']
                        or audit.get('node') != binding['node'] or audit.get('reused_from_audit')
                        or audit.get('native_task_id') not in permitted or audit.get('validated') is False
                        or audit.get('template_hash') != binding.get('template_hash')
                        or audit.get('schema_sha256') != fingerprint(output_schema)
                        or audit.get('input_sha256') != fingerprint(wire)
                        or audit.get('user_sha256') != fingerprint(wire['user'])
                        or audit.get('image_inputs', []) != image_inputs):
                    continue
                previous = permitted[audit['native_task_id']]
                if previous and (str(audit.get('native_run_id')) not in previous['ended_run_ids']
                                 or audit['template_hash'] != previous['template_hash']):
                    continue
                model = audit.get('model', {})
                if (model.get('origin') != 'hermes_native_model'
                        or model.get('response_sha256') != audit['response_sha256']):
                    continue
                session = model.get('session_id')
                if db.execute('SELECT source FROM sessions WHERE id=?', (session,)).fetchone() != ('kanban',):
                    continue
                rows = list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?', (session,)))
                if any(tools and tools != '[]' for _,_,tools in rows):
                    continue
                from revision_disposition import native_user
                users = [u for role,c,_ in rows if role == 'user'
                         and (u:=native_user(c,audit)) is not None and u == wire['user']]
                raw = [c for role,c,_ in rows if role == 'assistant' and fingerprint(c) == audit['response_sha256']]
                if len(users) != 1 or len(raw) != 1:
                    continue
                draft, _ = decode(raw[0])
                from hermes_source_units.validation import _check_shape
                _check_shape(output_schema,draft,{},'$')
                if 'blocked_reason' in draft:
                    continue
            except (ContractError, ValueError, TypeError, KeyError):
                continue
            adapter.worker_check(binding)
            replay = {**audit, 'native_task_id':binding['task_id'],
                'native_run_id':__import__('os').environ.get('HERMES_KANBAN_RUN_ID'),
                'model_elapsed_ms':0, 'receipt_refs':[],
                'reused_from_audit':path.relative_to(vault).as_posix(),
                'original_native_task_id':audit['native_task_id'],
                'recovery_contract':'same-node-exact-reducer-call/v1'}
            replay.pop('validated', None)
            ref = f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/semantic-calls/{fingerprint(replay)}.json'
            _write_atomic(_vault_path(vault, ref), _json_bytes(replay))
            return draft, ref
    return None


def recover(adapter, binding, phase, view, decode, system, *, home=None):
    """Existing native audit is the recovery interface, never edited domain data.

    A cancelled/superseded lease is not reused. The current native worker owns a
    fresh checked lease and re-enters normal bound submission with source/ACL checks.
    """
    home=Path(home) if home is not None else Path.home()/'.hermes'
    if not (home/'state.db').is_file(): return None
    vault=adapter.workflow.vault
    root=vault/'_system/ledgers/ingest-workflows'/binding['workflow_id']/'semantic-calls'
    checked=adapter.worker_check(binding)
    ids={f't{i}':tid for i,tid in enumerate(checked['task_ids'],1)}
    pending={ids[t['task']]:(t['task'],task_content(view,t)) for t in view['tasks']}
    key='drafts' if phase=='candidate' else 'reviews'
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        for path in sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            audit=_load_json(path)
            if audit.get('phase')!=phase or audit.get('workflow_id')!=binding['workflow_id'] or audit.get('reused_from_audit'):
                continue
            model=audit.get('model',{})
            if model.get('origin')!='hermes_native_model': continue
            session=model.get('session_id')
            if db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone()!=('kanban',):continue
            rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(session,)))
            if any(tools and tools!='[]' for _,_,tools in rows):continue
            users=[c for role,c,_ in rows if role=='user' and fingerprint(c)==audit['user_sha256']]
            raw=[c for role,c,_ in rows if role=='assistant' and fingerprint(c)==audit['response_sha256']]
            if len(users)!=1 or len(raw)!=1 or model.get('response_sha256')!=audit['response_sha256']:continue
            try:
                request_text=_request_text(users[0])
                body,end=json.JSONDecoder().raw_decode(request_text)
                if not isinstance(body,dict) or body.get('phase') != phase:continue
                old_view=body['input']
                expected_labels='\n'.join('Image '+str(n)+': '+', '.join(r['task']+'/'+r['material'] for r in image['references'])
                    for n,image in enumerate(audit.get('image_inputs',[]),1))
                expected_tail='\n'+expected_labels if expected_labels else ''
                if request_text[end:]!=expected_tail:continue
                # Hermes persists user/assistant only. The guarded native audit
                # binds exact system+user bytes; a prompt-version guess cannot pass.
                if audit.get('input_sha256')!=fingerprint({'system':system,'user':users[0]}):continue
                old_slice=adapter.workflow.knowledge._slice(checked['batch_id'],audit['node'].partition(':')[2])
                old_ids=audit.get('task_identities') or {f't{i}':tid for i,tid in enumerate(old_slice['task_ids'],1)}
                decoded,_=decode(raw[0])
                items=(slot_results(phase,decoded,old_view['tasks'])[key] if 'results' in decoded else decoded[key])
                recovered=[]
                for item in items:
                    validate(phase,{key:[item]})
                    tid=old_ids[item['task']]
                    if tid not in pending:continue
                    old_task=next(t for t in old_view['tasks'] if t['task']==item['task'])
                    alias,content=pending[tid]
                    if task_content(old_view,old_task)!=content:continue
                    if (phase=='citation' and old_task.get('candidate',{}).get('revision_review')
                            and item.get('decision')!='revised'):
                        continue  # Unchanged confirmation cannot fill a revision's missing inspections.
                    recovered.append({**item,'task':alias})
            except (ContractError,ValueError,TypeError,KeyError,StopIteration):continue
            if not recovered:continue
            replay={**audit,'native_task_id':binding['task_id'],'node':binding['node'],
                'native_run_id':__import__('os').environ.get('HERMES_KANBAN_RUN_ID'),
                'template_hash':binding.get('template_hash'),'model_elapsed_ms':0,'receipt_refs':[],
                'reused_from_audit':path.relative_to(vault).as_posix(),
                'original_native_task_id':audit['native_task_id'],
                'recovered_tasks':[r['task'] for r in recovered],
                'recovery_contract':'same-task-same-semantic-input/v1'}
            replay.pop('validated',None)
            ref=f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/semantic-calls/{fingerprint(replay)}.json'
            _write_atomic(_vault_path(vault,ref),_json_bytes(replay))
            return {key:recovered,'slot_failures':[]},ref
    return None
