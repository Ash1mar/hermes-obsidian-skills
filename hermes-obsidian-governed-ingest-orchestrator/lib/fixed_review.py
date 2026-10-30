"""Program Build acceptance with explicit page-local risks; legacy pins stay valid."""
import hashlib

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
import fixed_evidence


def fail(code,message):
    raise ContractError(code,'$',message)


def policy_enabled(adapter,binding):
    workflow=adapter.workflow.status(binding['workflow_id'])
    return '<!-- hermes-program-build-acceptance/v1 -->' in adapter.workflow.pinned_templates(workflow)['build-finalize']


def snapshot(adapter,binding,program=False):
    return read_snapshot(adapter,binding,adapter.worker_check(binding),program)


def read_snapshot(adapter,binding,checked,program=False):
    """Pure current materials; recovery authenticates its stopped binding first."""
    service=adapter.workflow.knowledge
    batch=service._batch(checked['batch_id']); workflow=adapter.workflow.status(binding['workflow_id'])
    registry=service.list_identities(); subjects={s['subject_id']:s for s in registry['subjects']}
    candidates,passes=service._pass_index(batch['task_ids'])
    if program and workflow['checkpoints']['checkpoint_1']['state']!='approved':
        fail('CHECKPOINT_BLOCKED','program acceptance requires the current approved checkpoint')
    backend={'batch_id':batch['batch_id'],'actor':batch['actor'],'run_ids':batch['run_ids'],
        'checkpoint':workflow['checkpoints']['checkpoint_1'],'registry':registry,
        'passes':[fingerprint(p) for tid in sorted(passes) for p in passes[tid]],'runs':[],'pages':{}}
    views=[]
    for run_id in batch['run_ids']:
        run=_load_json(service._run_path(run_id)); validate_record('build_run',run)
        if run['state']=='completed': continue
        if program:service.validate_run(run_id)
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
            evidence=[]; selected=[]
            for ref in decision['candidate_refs']:
                pair=candidates.get((ref['pass_id'],ref['candidate_id']))
                if pair is None or pair[0]['pass_kind']!='citation':
                    fail('STALE_INPUT','draft no longer has its active citation evidence')
                record,candidate=pair
                selected.append(pair)
                evidence.append({**{k:v for k,v in candidate.items() if k not in ('candidate_id','support_refs')},
                    'evidence_qa':[{k:v for k,v in i.items() if k!='source_ref'} for i in record['inspections']]})
            packet,readings=fixed_evidence.original_evidence(service,[r for r,c in selected],
                backend['actor'],service._document_registry_revision())
            for item,(record,candidate) in zip(evidence,selected):
                item['source_evidence']=[fixed_evidence.support(service,record,candidate,readings)]
            backend['pages'][alias]={'run_id':run_id,'page':page,'parent_hash':parent_hash}
            if program:
                from hermes_source_units.build_acceptance import assess
                backend['pages'][alias]['assessment']=assess(service,run,page,selected=selected,
                    parent_content=parent['content'] if parent else None)
            views.append({'page':alias,'identity':{k:v for k,v in decision['identity'].items() if k!='subject_id'},
                'path':page['path'],'content':raw.decode('utf-8'),'existing_page':parent,
                'qa_status':page['qa_status'],'business_status':page['business_status'],
                'visibility':page['visibility'],'citation_evidence':evidence,'original_evidence':packet})
    value={'contract':'fixed-review-input/v1','binding':dict(binding),'backend':backend,'views':views}
    value['input_id']=fingerprint(value)
    return value


def execute(adapter,binding,caller,model_call,attach_receipt):
    adapter.worker_begin(binding); program=policy_enabled(adapter,binding)
    value=snapshot(adapter,binding,True) if program else snapshot(adapter,binding)
    ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/review-{value['input_id']}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(value))
    reviews={}; acceptances={}; call_refs=[]; held=[]; dispositions=[]
    for view in value['views']:
        from fixed_semantic import HermesCaller,prepare_request_projection
        if isinstance(caller,HermesCaller):
            from fixed_page_evidence import select
            view=select(adapter,binding,'page-review',view,prepare_request_projection)
        from semantic_output import execute as checked_output,check,path_for
        assessment=value['backend']['pages'][view['page']]['assessment'] if program else None
        # Exact old journals, including refusals and unknown reservations, take
        # precedence. A policy change never erases a consumed review allowance.
        if program:
            from hermes_source_units.build_acceptance import record
            legacy_journal=path_for(adapter,binding,'page-review',view)
            if not legacy_journal.exists():
                if not assessment['risk_reasons']:
                    reviews[view['page']]='Program acceptance; no independent page review performed. Citation/Reduce evidence reused; no semantic or publication approval inferred.'
                    acceptances[view['page']]=record(assessment)
                    from build_disposition import item
                    dispositions.append(item(adapter,binding,value,view,state='ready'))
                    continue
                view={**view,'review_scope':{'policy':assessment['policy'],
                    'risk_reasons':assessment['risk_reasons'],
                    'instruction':'Review only the named page risks against its assigned candidates, originals and parent. Do not repeat candidate discovery or review unrelated reading-package coverage.'}}
        result=None;call_ref=None
        try:
            result,call_ref,_=checked_output(adapter,binding,'page-review',view,caller,model_call,
                lambda d:check('page-review',None,None,None,d))
            if result['decision']!='approved':fail('SEMANTIC_REVIEW_REQUIRED',result['review_note'])
        except ContractError as exc:
            from fixed_page_work import LOCAL
            if exc.code not in LOCAL:raise
            held.append({'page':view['page'],'code':exc.code,'reason':str(exc)})
            if program:
                from build_disposition import item
                dispositions.append(item(adapter,binding,value,view,state='deferred',result=result,error=exc,
                    audit_ref=call_ref or getattr(exc,'semantic_audit_ref',None)))
            continue
        reviews[view['page']]=result['review_note']; call_refs.append(call_ref)
        if program:acceptances[view['page']]=record(assessment,review_audit_ref=call_ref)
        if program:
            from build_disposition import item
            dispositions.append(item(adapter,binding,value,view,state='ready',result=result,audit_ref=call_ref))
    if program:
        from build_disposition import persist
        disposition_ref=persist(adapter,binding,value,ref,dispositions,reviews,acceptances)
        report=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/reports/build-acceptance-disposition-v1-{value['input_id']}.json"
        _write_atomic(_vault_path(adapter.workflow.vault,report),_json_bytes({
            'contract':'program-build-acceptance-report/v1','input_id':value['input_id'],
            'acceptances':acceptances,'held':held,'committed':False,'disposition_ref':disposition_ref,
            'limitations':'Program acceptance does not prove semantic correctness; QA, coverage, business and release gates remain separate.'}))
    if held:
        suffix='-disposition-v1' if program else ''
        report=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/reports/page-review-held{suffix}-{value['input_id']}.json"
        _write_atomic(_vault_path(adapter.workflow.vault,report),_json_bytes({'held':held,'accepted_review_calls':call_refs,'finalized':False}))
        fail('SEMANTIC_REVIEW_REQUIRED','Independent valid reviews preserved; inspect '+report+
            ('; prepared/deferred evidence: '+disposition_ref if program else ''))
    backend=value['backend']; finalizations=[]
    for run in backend['runs']:
        finalizations.append({'run_id':run['run_id'],'actor':backend['actor'],'expected_revision':run['revision'],
            'reviews':[{'page_id':p['page']['page_id'],'authored_sha256':p['page']['authored_sha256'],
                'parent_authored_sha256':p['parent_hash'],'actor':backend['actor'],'note':reviews[alias],
                **({'acceptance':acceptances[alias]} if program else {})}
                for alias,p in backend['pages'].items() if p['run_id']==run['run_id']]})
    if finalizations:
        with worker_binding(binding),workflow_write_guard(adapter.workflow.vault,kinds=('build-finalize',),actor=backend['actor']):
            fresh=snapshot(adapter,binding,True) if program else snapshot(adapter,binding)
            if fresh['input_id']!=value['input_id']:
                fail('STALE_INPUT','reviewed input changed; no automatic approval or rebind')
            result=adapter.workflow.knowledge.finalize_batch({'batch_id':backend['batch_id'],
                'actor':backend['actor'],'finalizations':finalizations})
        receipt_ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/worker-receipts/finalize-{fingerprint(result)}.json"
        _write_atomic(_vault_path(adapter.workflow.vault,receipt_ref),_json_bytes(result))
        for call_ref in call_refs: attach_receipt(adapter,call_ref,{'validated':result['ok'],'receipt_ref':receipt_ref})
        if not result['ok']: fail('FINALIZATION_FAILED','inspect durable failures; preserve committed valid pages')
    return adapter.worker_complete(binding)
