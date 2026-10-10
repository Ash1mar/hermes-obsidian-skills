"""Reuse genuine audited responses only against identical fresh reading authority."""
import copy
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from fixed_semantic_schema import validate, slot_results


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
                request_text=users[0].partition('\n')[2]
                body,end=json.JSONDecoder().raw_decode(request_text);old_view=body['input']
                expected_labels='\n'.join('Image '+str(n)+': '+', '.join(r['task']+'/'+r['material'] for r in image['references'])
                    for n,image in enumerate(audit.get('image_inputs',[]),1))
                if request_text[end:].strip()!=expected_labels:continue
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
