"""Automatic no-call oversize repair, exact evidence round trips and one work allowance."""
import copy,hashlib,re
from pathlib import Path
from hermes_source_units.source_units import _write_atomic,_json_bytes,_load_json,_vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.partial_finalize import pin,pinned
from hermes_source_units.finalize_revision_state import load,work_id,fail
from finalize_disposition_control import inspect,apply as apply_control,material_identity

REASON='Verified lossless request representation repair. Preserve all page risks, original evidence, citations, conditions and QA; perform one independent review only.'
REVIEW_INSTRUCTION='Review only assigned page risks against all supplied originals; retain every necessary condition, citation and QA limit.'

def candidates(adapter,w,inventory,snapshot):
    from fixed_page_evidence import CONTRACT,COMPACT_CONTRACT,present_compact,restore_compact
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    from finalize_revision import view_with_control
    controls=load(adapter.workflow.vault,w['workflow_id']);eligible=[];deferred=[]
    for item in inventory['items']:
        if item['state']!='deferred':continue
        key=work_id(item['logical_work']['run_id'],item['logical_work']['page_id'])
        work=controls['works'].get(key,{})
        if (item['observed_status']!='execution_held' or (item.get('diagnostic') or {}).get('code')!='READING_WINDOW_OVERSIZE'
                or item['history']['attempt_count'] or work.get('grants')):
            continue
        view=copy.deepcopy(next(v for v in snapshot['views'] if v['page']==item['page']))
        assessment=item['evidence']['assessment']
        review_scope={'policy':assessment['policy'],'risk_reasons':assessment['risk_reasons'],'instruction':REVIEW_INSTRUCTION}
        grant={'evidence_encoding':COMPACT_CONTRACT,'review_scope':review_scope,
            'basis':{'kind':'verified_program_fix','reason':REASON}}
        probe=view_with_control(view,{'original_revision_id':item['logical_work']['revision_id'],'grants':[grant]})
        if restore_compact(present_compact(probe))!=probe:
            fail('lossless repair did not round-trip; stop before any model call','PROGRAM_REPAIR_INVALID')
        images=select_images(probe['original_evidence']['tasks'],image_catalog(adapter,probe['original_evidence']))
        encoded=prepare_request_projection('page-review',probe,schema_for('page-review',probe),images)
        old={**view,'evidence_encoding':CONTRACT,'review_scope':review_scope}
        original=prepare_request_projection('page-review',old,schema_for('page-review',old),images)
        row={'issue_id':item['issue_id'],'work_id':key,'run_id':item['logical_work']['run_id'],
            'page_id':item['logical_work']['page_id'],'material_sha256':material_identity(view),
            'original_input_size':original['input_size'],'repaired_complete_input_size':encoded['input_size'],
            'repaired_wire_sha256':fingerprint(encoded['wire']),'roundtrip_exact':True,
            'attempt_count':0,'maximum_new_calls':1,'encoding':COMPACT_CONTRACT}
        (eligible if encoded['input_size']<=30000 and original['input_size']>encoded['input_size'] else deferred).append(row)
    return {'eligible':eligible,'still_oversize':deferred,'rule':'zero previous calls; exact round trip; complete request <=30000; one independent review per logical work; no semantic retries'}

def authorize(adapter,request,*,dry_run=False):
    """One trusted operator policy invocation, no per-page onsite decisions."""
    adapter.workflow._check_request(request)
    op=request.get('operation_id','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',op):fail('stable program-repair operation required')
    vault=adapter.workflow.vault;wid=request['workflow_id']
    root=vault/f'_system/ledgers/ingest-workflows/{wid}/program-repair-operations/{op}'
    receipt_path=root/'receipt.json';plan_path=root/'plan.json'
    if receipt_path.exists():
        saved=_load_json(receipt_path)
        if saved['request_digest']!=request['input_digest']:fail('program-repair operation ID changed','IDEMPOTENCY_CONFLICT')
        return {**saved,'replayed':True}
    if plan_path.exists():
        plan=_load_json(plan_path)
        if plan['request_digest']!=request['input_digest']:fail('program-repair plan changed','IDEMPOTENCY_CONFLICT')
        pinned(vault,plan['report'])
    else:
        w,inventory,snapshot,checks=inspect(adapter,request)
        controls=load(vault,wid)
        if controls['revision']!=request['expected_control_revision']:fail('control revision changed','REVISION_CONFLICT')
        analysis=candidates(adapter,w,inventory,snapshot)
        report={'contract':'automatic-finalize-program-repair/v1','workflow_id':wid,'inventory':request['inventory'],
            'snapshot':inventory['snapshot'],'evidence_checks_sha256':fingerprint(checks),**analysis,
            'code_fingerprints':{n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest()
                for n in ('fixed_page_evidence.py','fixed_semantic.py','finalize_revision.py','finalize_program_repair.py')}}
        ref=f'_system/ledgers/ingest-workflows/{wid}/program-repairs/{fingerprint(report)}.json'
        pointer={'ref':ref,'sha256':hashlib.sha256(_json_bytes(report)).hexdigest()}
        planned=[];revision=controls['revision']
        for row in analysis['eligible']:
            basis={'kind':'verified_program_fix','reason':REASON,'evidence':[pointer]}
            for action in ('reopen','revise'):
                child={'workflow_id':wid,'actor':w['actor'],'expected_revision':w['revision'],
                    'expected_control_revision':revision,'operation_id':op+'-'+row['work_id'][:12]+'-'+action,
                    'run_id':row['run_id'],'inventory':request['inventory'],'issue_id':row['issue_id'],
                    'action':action,'basis':basis,'reason':REASON}
                if action=='revise':child.update(mode='review_only',evidence_encoding=row['encoding'])
                from hermes_source_units import mutation_digest
                child['input_digest']=mutation_digest(child);planned.append(child);revision+=1
        plan={'request_digest':request['input_digest'],'request':request,'report':pointer,'requests':planned,
            'eligible':len(analysis['eligible']),'still_oversize':len(analysis['still_oversize'])}
        if not dry_run:
            _write_atomic(_vault_path(vault,ref),_json_bytes(report));_write_atomic(plan_path,_json_bytes(plan))
    if not dry_run:
        for child in plan['requests']:apply_control(adapter,child)
    result={'ok':True,'request_digest':request['input_digest'],'report':plan['report'],
        'dry_run':dry_run,'works_authorized':plan['eligible'],'maximum_new_calls':plan['eligible'],
        'still_oversize':plan['still_oversize'],'resumed':False,'domain_calls_made':0}
    if not dry_run:_write_atomic(receipt_path,_json_bytes(result))
    return result
