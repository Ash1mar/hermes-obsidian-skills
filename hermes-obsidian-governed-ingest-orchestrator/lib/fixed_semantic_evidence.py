"""Read actual native semantic sessions and program preflight receipts."""
import json
import sqlite3
from pathlib import Path

from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint, validate_record
from ingest_kanban import semantic_template


def pass_evidence(home, adapter, workflow, native_task_id):
    cards=[c for c in workflow['kanban']['task_map'] if c['task_id']==native_task_id]
    if len(cards)!=1 or not cards[0]['node'].startswith('pass-slice:'):
        raise RuntimeError('fixed semantic audit has no current native Pass card')
    card=cards[0]; sid=card['node'].partition(':')[2]
    content=(adapter.workflow.execution_template(workflow,sid)
        or adapter.workflow.pinned_templates(workflow)['pass-slice'])
    if adapter.workflow.revision_allows(workflow,card['node']):
        content=adapter.workflow.revision_instructions(workflow)
    if not semantic_template('pass-slice',content):
        return None
    vault=adapter.workflow.vault; service=adapter.workflow.knowledge
    owned=set(service._slice(workflow['batch_id'],sid)['task_ids'])
    sessions=set(); seen=set(); preflights=set()
    root=_vault_path(vault,f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/semantic-calls")
    with sqlite3.connect('file:'+str(Path(home)/'state.db')+'?mode=ro',uri=True) as db:
        for path in root.glob('*.json'):
            audit=_load_json(path)
            if audit.get('native_task_id')!=native_task_id: continue
            model=audit['model']; session=model.get('session_id')
            if (audit['workflow_id']!=workflow['workflow_id'] or audit['node']!=card['node']
                    or audit['input_codepoints']>audit['limit']
                    or model.get('origin')!='hermes_native_model' or not model.get('api_calls')
                    or model.get('response_sha256')!=audit['response_sha256']):
                raise RuntimeError('fixed semantic call lacks genuine bounded native provenance')
            source=db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone()
            rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(session,)))
            if (source!=('kanban',) or any(c and c!='[]' for _,_,c in rows)
                    or not any(r=='user' and fingerprint(c)==audit['user_sha256'] for r,c,_ in rows)
                    or not any(r=='assistant' and fingerprint(c)==audit['response_sha256'] for r,c,_ in rows)):
                raise RuntimeError('fixed semantic audit does not match its actual tool-free native transcript')
            sessions.add(session)
            for ref in audit['receipt_refs']:
                receipt=_load_json(_vault_path(vault,ref))
                for result in receipt.get('results',[]):
                    tid=result['task_id']; preflight_ref=result.get('preflight_ref')
                    if tid not in owned or not preflight_ref:
                        raise RuntimeError('fixed semantic receipt lacks owned bound preflight')
                    preflight=_load_json(_vault_path(vault,preflight_ref))
                    record=_load_json(_vault_path(vault,result['path']))
                    validate_record('knowledge_pass',record)
                    expected='candidate' if audit['phase']=='candidate' else 'citation'
                    bound=preflight['binding']
                    if (preflight.get('contract')!='hermes-bound-pass-preflight/v1'
                            or not preflight.get('validated') or preflight['pass_id']!=record['pass_id']
                            or record['pass_id']!=result['pass_id'] or record['task_id']!=tid
                            or record['pass_kind']!=expected or bound['workflow_id']!=workflow['workflow_id']
                            or bound['node']!=card['node'] or bound['worker_id']!='ingest-worker-'+native_task_id
                            or preflight['template_hash']!=record.get('template_hash')):
                        raise RuntimeError('fixed semantic preflight differs from its persisted native Pass')
                    seen.add((tid,expected)); preflights.add(preflight_ref)
    if not sessions or any((tid,'citation') not in seen for tid in owned):
        raise RuntimeError('fixed semantic Pass lacks complete genuine citation/preflight evidence')
    return {'task_id':native_task_id,'session_ids':sorted(sessions),
        'origin':'native_fixed_semantic','preflight_tool_calls':0,
        'program_preflight_receipts':len(preflights),'citation_tasks':len(owned)}
