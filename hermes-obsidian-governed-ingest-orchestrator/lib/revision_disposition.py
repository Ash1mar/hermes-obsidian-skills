"""Authenticate native review attempts before explicit partial-revision disposition."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint
from fixed_semantic_recovery import task_content, _request_text


def fail(code,message):raise ContractError(code,'$',message)


def evidence(view,task):
    task={k:copy.deepcopy(v) for k,v in task.items()
          if k not in ('candidate','continuation','input_id','next_sequence')}
    # Packet-wide QA prose may concern a different task; material QA stays bound.
    return task_content({**view,'qa_rule':None},task)


def native_user(content,call):
    if fingerprint(content)==call['user_sha256']:return content
    # Hermes appends one literal screenshot marker per persisted image part.
    count=len(call.get('image_inputs',[]));suffix='\n[screenshot]'*count
    if count and content.endswith(suffix) and fingerprint(content[:-len(suffix)])==call['user_sha256']:
        return content[:-len(suffix)]
    return None


def verify(adapter,value,request,*,home=None):
    """No model calls. Uncalled work cannot be excluded or repeat failed calls."""
    revision=value['pass_revision'];targets={t['task_id']:t for t in revision['tasks']}
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{value["workflow_id"]}'
    contexts={}
    for p in (root/'native-inputs').glob('*.json'):
        c=_load_json(p);owner=c.get('owner',{})
        if owner.get('workflow_id')==value['workflow_id']:
            contexts.setdefault((owner.get('task_id'),owner.get('node')),[]).append(c)
    checked={};called=set();live={};k=adapter.workflow.knowledge
    home=Path(home) if home is not None else Path.home()/'.hermes'
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        for path in sorted((root/'semantic-calls').glob('*.json')):
            call=_load_json(path)
            if call.get('phase')!='citation' or call.get('reused_from_audit'):continue
            relevant={a:t for a,t in call.get('task_identities',{}).items() if t in targets}
            if not relevant:continue
            if call.get('workflow_id')!=value['workflow_id']:fail('STALE_INPUT','native citation workflow differs')
            matches=[c for c in contexts.get((call['native_task_id'],call['node']),[])
                     if c['owner'].get('actor')==value['actor'] and c['owner'].get('template_hash')==call['template_hash']
                     and any(c.get('candidates',{}).get(a)==targets[t]['pass_id'] for a,t in relevant.items())]
            if not matches:continue
            if len(matches)!=1:fail('STALE_INPUT','ambiguous original native revision context')
            context=matches[0];packet_path=_vault_path(k.vault,context['packet_ref'])
            if hashlib.sha256(packet_path.read_bytes()).hexdigest()!=context['packet_sha256']:
                fail('SOURCE_CHANGED','original native revision packet changed')
            packet=_load_json(packet_path);model=call['model'];session=model['session_id']
            if (model.get('origin')!='hermes_native_model' or model.get('response_sha256')!=call['response_sha256']
                    or db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone()!=('kanban',)):
                fail('STALE_INPUT','untrusted native citation call')
            messages=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(session,)))
            users=[u for r,c,_ in messages if r=='user' and (u:=native_user(c,call)) is not None]
            responses=[c for r,c,_ in messages if r=='assistant' and fingerprint(c)==call['response_sha256']]
            if len(users)!=1 or len(responses)!=1 or any(t and t!='[]' for _,_,t in messages):
                fail('STALE_INPUT','native request/response hashes or tool authority differ')
            text=_request_text(users[0]);body,end=json.JSONDecoder().raw_decode(text);view=body['input']
            labels='\n'.join('Image '+str(n)+': '+', '.join(r['task']+'/'+r['material'] for r in i['references'])
                             for n,i in enumerate(call.get('image_inputs',[]),1))
            if body.get('phase')!='citation' or text[end:]!=('\n'+labels if labels else ''):
                fail('STALE_INPUT','native citation request framing differs')
            from fixed_semantic_media import image_catalog,select_images
            images=select_images(view['tasks'],image_catalog(adapter,view))
            if [{k:v for k,v in i.items() if k!='url'} for i in images]!=call.get('image_inputs',[]):
                fail('SOURCE_CHANGED','native image authority differs from registered assets')
            snapshot=adapter.kanban.task_snapshot(value['kanban']['board_id'],call['native_task_id'])
            runs=[r for r in snapshot.get('runs',[]) if str(r['id'])==str(call['native_run_id'])]
            if (snapshot['task'].get('worker_pid') or snapshot['task'].get('current_run_id')
                    or len(runs)!=1 or not runs[0].get('ended_at') or not runs[0].get('worker_pid')):
                fail('WORKER_ACTIVE','citation disposition requires an ended actual native attempt')
            slots={t['task']:t for t in view['tasks']};authenticated=[]
            for alias,tid in relevant.items():
                if alias not in slots or context.get('candidates',{}).get(alias)!=targets[tid]['pass_id']:continue
                original=next(t for t in packet['tasks'] if t['task']==alias)
                candidate=copy.deepcopy(original['continuation']['candidate']);candidate.pop('candidate_pass_id',None)
                if not candidate.get('revision_review') or slots[alias].get('candidate')!=candidate:
                    fail('STALE_INPUT','observed native predecessor differs from this revision')
                if tid not in live:
                    target=targets[tid]
                    package=_load_json(_vault_path(k.vault,f'_system/knowledge-builds/task-{tid}/readings/{target["reading_package_id"]}.json'))
                    projection=k.model_input(tid,package,persist=False)
                    projected=k.model_packet([projection],value['batch_id'],persist=False)['view']
                    live[tid]=evidence(projected,projected['tasks'][0])
                if evidence(view,slots[alias])!=live[tid]:fail('SOURCE_CHANGED','native review evidence differs from current authorized material')
                authenticated.append(tid);called.add(tid)
            if authenticated:
                checked[path.relative_to(k.vault).as_posix()]={'task_ids':authenticated,'audit_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'native_task_id':call['native_task_id'],'native_run_id':call['native_run_id'],
                    'packet_ref':context['packet_ref'],'packet_sha256':context['packet_sha256'],
                    'response_sha256':call['response_sha256'],'user_sha256':call['user_sha256']}
    proofs={};removed={e['task_id'] for e in request['entries']}
    for entry in request['entries']:
        tid=entry['task_id'];ref=entry.get('native_audit_ref','');proof=checked.get(ref)
        if entry.get('disposition_kind') not in ('unverifiable_material','unverified_model_result'):
            fail('INVALID_SCOPE','name the material limitation or unverified model result')
        if not proof or tid not in proof['task_ids']:fail('SKIP_NOT_ALLOWED','uncalled or unauthenticated revision work cannot be discarded')
        proofs[tid]={**proof,'native_audit_ref':ref,'disposition_kind':entry['disposition_kind']}
    continuations=[];uncalled=[]
    for sid in revision['slice_ids']:
        item=k._slice(value['batch_id'],sid)
        pending=[t['task_id'] for t in revision['tasks'] if t['task_id'] in item['task_ids'] and t['task_id'] not in removed
                 and not any(p.get('revision_id')==revision['revision_id'] for p in k._pass_index([t['task_id']])[1][t['task_id']])]
        if not pending:continue
        if set(pending)&called:fail('RECOVERY_NOT_ALLOWED','called unfinished work must be separately resolved, never silently retried')
        card=next(c for c in value['kanban']['task_map'] if c['node']=='pass-slice:'+sid)
        failure=_load_json(root/f'reports/failed-{fingerprint(card["node"])[:16]}.json')
        if (failure.get('task_id')!=card['task_id'] or failure.get('idempotency_key')!=card['idempotency_key']
                or failure.get('error_code') not in ('SEMANTIC_DRAFT_REJECTED','SEMANTIC_REVIEW_REQUIRED','INVALID_RANGE')):
            fail('RECOVERY_NOT_ALLOWED','only ended semantic/range refusal may release its uncalled remainder')
        continuations.append(sid);uncalled.extend(pending)
    return {'entries':proofs,'continuation_slice_ids':continuations,'uncalled_task_ids':uncalled}
