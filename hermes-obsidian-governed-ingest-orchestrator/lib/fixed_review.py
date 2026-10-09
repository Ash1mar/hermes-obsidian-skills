"""Bounded semantic page reviews; program owns Finalize identities and writes."""
import hashlib

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


def fail(code,message):
    raise ContractError(code,'$',message)


def snapshot(adapter,binding):
    checked=adapter.worker_check(binding); service=adapter.workflow.knowledge
    batch=service._batch(checked['batch_id']); workflow=adapter.workflow.status(binding['workflow_id'])
    registry=service.list_identities(); subjects={s['subject_id']:s for s in registry['subjects']}
    candidates,passes=service._pass_index(batch['task_ids'])
    backend={'batch_id':batch['batch_id'],'actor':batch['actor'],'run_ids':batch['run_ids'],
        'checkpoint':workflow['checkpoints']['checkpoint_1'],'registry':registry,
        'passes':[fingerprint(p) for tid in sorted(passes) for p in passes[tid]],'runs':[],'pages':{}}
    views=[]
    for run_id in batch['run_ids']:
        run=_load_json(service._run_path(run_id)); validate_record('build_run',run)
        if run['state']=='completed': continue
        backend['runs'].append(run)
        decisions={d['page_id']:d for d in run['decisions']}
        for page in run['page_revisions']:
            path=_vault_path(service.vault,f"_system/knowledge-builds/{run_id}/pages/{page['page_id']}.md")
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=page['authored_sha256']:
                fail('STALE_INPUT','page draft differs from its recorded revision')
            alias='e'+str(len(views)+1); decision=decisions[page['page_id']]
            parent=None; parent_hash=None
            if page['parent_revision_id']:
                subject=subjects.get(page['subject_id'])
                if not subject or subject['current_revision_id']!=page['parent_revision_id']:
                    fail('STALE_INPUT','review parent identity changed')
                parent_record=_load_json(_vault_path(service.vault,
                    f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['parent_revision_id']}.json"))
                validate_record('page_revision',parent_record)
                prior=_vault_path(service.vault,subject['current_path']).read_bytes()
                parent_hash=hashlib.sha256(prior).hexdigest()
                if parent_hash!=parent_record['authored_sha256']:
                    fail('STALE_INPUT','review parent content changed')
                parent={'path':subject['current_path'],'content':prior.decode('utf-8')}
            evidence=[]
            for ref in decision['candidate_refs']:
                pair=candidates.get((ref['pass_id'],ref['candidate_id']))
                if pair is None or pair[0]['pass_kind']!='citation':
                    fail('STALE_INPUT','draft no longer has its active citation evidence')
                record,candidate=pair
                evidence.append({**{k:v for k,v in candidate.items() if k not in ('candidate_id','support_refs')},
                    'evidence_qa':[{k:v for k,v in i.items() if k!='source_ref'} for i in record['inspections']]})
            backend['pages'][alias]={'run_id':run_id,'page':page,'parent_hash':parent_hash}
            views.append({'page':alias,'identity':{k:v for k,v in decision['identity'].items() if k!='subject_id'},
                'path':page['path'],'content':raw.decode('utf-8'),'existing_page':parent,
                'qa_status':page['qa_status'],'business_status':page['business_status'],
                'visibility':page['visibility'],'citation_evidence':evidence})
    value={'contract':'fixed-review-input/v1','binding':dict(binding),'backend':backend,'views':views}
    value['input_id']=fingerprint(value)
    return value


def execute(adapter,binding,caller,model_call,attach_receipt):
    adapter.worker_begin(binding); value=snapshot(adapter,binding)
    ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/review-{value['input_id']}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(value))
    reviews={}; call_refs=[]
    for view in value['views']:
        result,call_ref=model_call(adapter,binding,'page-review',view,caller,30000)
        if result['decision']!='approved':
            fail('SEMANTIC_REVIEW_REQUIRED',result['review_note'])
        reviews[view['page']]=result['review_note']; call_refs.append(call_ref)
    backend=value['backend']; finalizations=[]
    for run in backend['runs']:
        finalizations.append({'run_id':run['run_id'],'actor':backend['actor'],'expected_revision':run['revision'],
            'reviews':[{'page_id':p['page']['page_id'],'authored_sha256':p['page']['authored_sha256'],
                'parent_authored_sha256':p['parent_hash'],'actor':backend['actor'],'note':reviews[alias]}
                for alias,p in backend['pages'].items() if p['run_id']==run['run_id']]})
    if finalizations:
        with worker_binding(binding),workflow_write_guard(adapter.workflow.vault,kinds=('build-finalize',),actor=backend['actor']):
            if snapshot(adapter,binding)['input_id']!=value['input_id']:
                fail('STALE_INPUT','reviewed input changed; no automatic approval or rebind')
            result=adapter.workflow.knowledge.finalize_batch({'batch_id':backend['batch_id'],
                'actor':backend['actor'],'finalizations':finalizations})
        receipt_ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/worker-receipts/finalize-{fingerprint(result)}.json"
        _write_atomic(_vault_path(adapter.workflow.vault,receipt_ref),_json_bytes(result))
        for call_ref in call_refs: attach_receipt(adapter,call_ref,{'validated':result['ok'],'receipt_ref':receipt_ref})
        if not result['ok']: fail('FINALIZATION_FAILED','inspect durable failures; preserve committed valid pages')
    return adapter.worker_complete(binding)
