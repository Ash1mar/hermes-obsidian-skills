"""Explicit exclusion of failed Pass work, with durable coverage gaps."""
from __future__ import annotations
import ast,copy,hashlib,json,os,re
from collections import Counter
from .source_units import _exclusive_lock,_json_bytes,_load_json,_vault_path,_write_atomic
from .validation import ContractError,fingerprint,validate_record

def fail(code,message):
    raise ContractError(code,'$',message)

def skippable_failure(report):
    code=report.get('error_code')
    if code in ('SEMANTIC_DRAFT_REJECTED','SEMANTIC_REVIEW_REQUIRED','OUTSIDE_READING_PACKAGE'):
        return code
    if code=='PROGRAM_EXECUTION_FAILED' and report.get('reason')=='native model request did not complete; inspect its session':
        return 'UNRESOLVED_NATIVE_MODEL_REQUEST'
    if code=='PASS_EXECUTION_BLOCKED':
        prefix='PASS_EXECUTION_BLOCKED at $: '
        try:items=ast.literal_eval(report['reason'][len(prefix):]) if report['reason'].startswith(prefix) else None
        except (SyntaxError,ValueError):items=None
        if isinstance(items,list) and items and all(isinstance(i,dict) and i.get('stage')=='pass'
                and i.get('code') in ('DUPLICATE','INCOMPLETE_COVERAGE','OUTSIDE_READING_PACKAGE') for i in items):
            return 'UNVERIFIED_CONTENT_OR_INSPECTION'
    fail('SKIP_NOT_ALLOWED','program budget, cancellation, permission and unknown failures require repair')

class PassSkipMixin:
    def _pass_skip_journal(self,wid):
        return _vault_path(self.vault,f'_system/ledgers/ingest-workflows/{wid}/pass-skip.pending.json')

    @staticmethod
    def skipped_pass_slices(value):
        return {sid for r in value.get('pass_skip_history',[]) for sid in r['slice_ids']}

    @staticmethod
    def skipped_pass_tasks(value):
        return {tid for r in value.get('pass_skip_history',[]) for tid in r['task_ids']}

    def verify_pass_skips(self,value):
        if self._pass_skip_journal(value['workflow_id']).exists():
            fail('PASS_SKIP_PENDING','finish the same skip request before execution')
        seen=set();reports=[]
        for pointer in value.get('pass_skip_history',[]):
            path=_vault_path(self.vault,pointer['report_ref']);data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=pointer['report_sha256']:
                fail('STALE_INPUT','Pass gap report changed')
            report=json.loads(data)
            if (report['request']['workflow_id']!=value['workflow_id'] or report['batch_id']!=value['batch_id']
                    or report['request']['actor']!=value['actor'] or report['request']['input_digest']!=pointer['input_digest']
                    or [e['slice_id'] for e in report['entries']]!=pointer['slice_ids']
                    or report['task_ids']!=pointer['task_ids'] or seen.intersection(pointer['task_ids'])):
                fail('STALE_INPUT','Pass gap identity/coverage changed')
            for entry in report['entries']:
                item=self.knowledge._slice(value['batch_id'],entry['slice_id'])
                if item['task_ids']!=entry['task_ids'] or item['input_fingerprint']!=entry['slice_input_fingerprint']:
                    fail('STALE_INPUT','skipped slice ownership changed')
                for task_id,expected in entry['target_fingerprints'].items():
                    task=self.knowledge._task(task_id)
                    if task['status']!='skipped' or fingerprint(task['target_refs'])!=expected:
                        fail('STALE_INPUT','skipped task status or target changed')
            seen.update(pointer['task_ids']);reports.append(report)
        return reports

    def skip_pass_slices(self,request,*,dry_run=False):
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            fail('ACCESS_DENIED','Pass skipping requires a trusted operator')
        self._check_request(request);wid=str(request['workflow_id']);op=request.get('operation_id','')
        if not isinstance(op,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',op):
            fail('INVALID_SCHEMA','invalid skip operation ID')
        entries=request.get('entries',[])
        if not entries or len({e['slice_id'] for e in entries})!=len(entries):
            fail('INVALID_SCHEMA','select unique failed slices')
        with _exclusive_lock(self._lock(wid)):
            value=self.status(wid)
            if value['actor']!=request['actor']:fail('ACTOR_MISMATCH','skip actor differs')
            journal_path=self._pass_skip_journal(wid)
            old=next((r for r in value.get('pass_skip_history',[]) if r['operation_id']==op),None)
            if old:
                if old['input_digest']!=request['input_digest']:fail('IDEMPOTENCY_CONFLICT','skip operation already used')
                if journal_path.exists():
                    journal=_load_json(journal_path)
                    if journal['request']!=dict(request):fail('IDEMPOTENCY_CONFLICT','pending skip request differs')
                    for item in journal['updates']:
                        if _vault_path(self.vault,item['ref']).read_bytes()!=_json_bytes(item['after']):
                            fail('STALE_INPUT','pending skip task changed')
                    journal_path.unlink()
                self.verify_pass_skips(value)
                return {'ok':True,'applied':False,'replayed':True,'revision':value['revision'],'report_ref':old['report_ref']}
            value=self._mutation(request)
            if (value['state']!='cancelled' or not value['cancel_requested'] or value['current_stage']!='analyzing'
                    or not value['batch_id'] or self.pending_pause(value) or value['dispatch_policy']['mode']!='full'):
                fail('INVALID_TRANSITION','skip requires a cancelled released Pass phase')
            if value.get('pass_revision') and value['pass_revision']['state']!='accepted':
                fail('REVISION_REVIEW_REQUIRED','finish semantic revision review first')
            service=self.knowledge
            with _exclusive_lock(service._batch_lock_path(value['batch_id'])):
                batch=service._batch(value['batch_id']);slices=service._slices(value['batch_id'])
                if not batch['cancel_requested'] or any(s['lease']['worker_id'] for s in slices):
                    fail('WORKER_ACTIVE','skip requires a cancelled batch without leases')
                if Counter(t for s in slices for t in s['task_ids'])!=Counter(batch['task_ids']):
                    fail('INCOMPLETE_COVERAGE','active slices must own every canonical task exactly once')
                self._verify_execution_evidence(value)
                if journal_path.exists():
                    journal=_load_json(journal_path)
                    if journal['request']!=dict(request):fail('IDEMPOTENCY_CONFLICT','finish the same pending skip')
                    if dry_run:fail('PASS_SKIP_PENDING','apply the same request to finish pending skip')
                    report=journal['report'];updates=journal['updates']
                else:
                    self.assert_execution_ready(value)
                    audited=[];updates=[];task_ids=[]
                    previously=self.skipped_pass_slices(value)
                    for entry in entries:
                        item=service._slice(value['batch_id'],entry['slice_id'])
                        if (item['slice_id'] not in {s['slice_id'] for s in slices} or item['state']!='cancelled'
                                or item['slice_id'] in previously or not entry.get('reason','').strip()):
                            fail('INVALID_TRANSITION','only cancelled unskipped failed work may be excluded')
                        proofs=[];covered=set()
                        for evidence in entry['evidence']:
                            path=_vault_path(self.vault,evidence['ref'])
                            expected_root=self.vault/f'_system/ledgers/ingest-workflows/{wid}/reports'
                            if path.parent!=expected_root or not path.name.startswith('failed-'):
                                fail('ACCESS_DENIED','skip evidence must be this workflow failure report')
                            data=path.read_bytes();failure=json.loads(data)
                            if hashlib.sha256(data).hexdigest()!=evidence['sha256']:fail('STALE_INPUT','skip failure changed')
                            category=skippable_failure(failure)
                            node=failure.get('node','');key=failure.get('idempotency_key','')
                            if not node.startswith('pass-slice:') or key!=f"ingest:{wid}:pass-slice:{failure.get('input_fingerprint')}":
                                fail('STALE_INPUT','failure is not a bound Pass attempt')
                            old_slice=service._slice(value['batch_id'],node.partition(':')[2])
                            binding=_load_json(_vault_path(self.vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json'))
                            if any(binding.get(k)!=v for k,v in {'workflow_id':wid,'node':node,
                                'task_id':failure.get('task_id'),'input_fingerprint':failure.get('input_fingerprint'),
                                'template_hash':old_slice['template_hash']}.items()):
                                fail('STALE_INPUT','failure binding changed')
                            covered.update(set(item['task_ids']).intersection(old_slice['task_ids']))
                            proofs.append({'ref':evidence['ref'],'sha256':evidence['sha256'],'category':category,'failure':failure})
                        if not proofs or not covered or (covered!=set(item['task_ids']) and entry.get('scope')!='whole_failed_slice'):
                            fail('INCOMPLETE_COVERAGE','partial failure evidence requires an explicit whole unfinished slice exclusion')
                        targets={}
                        for tid in item['task_ids']:
                            task=service._task(tid)
                            if task['actor']!=value['actor'] or task['status'] in ('completed','skipped'):
                                fail('STALE_INPUT','skip task owner or status changed')
                            after=copy.deepcopy(task);after.update(status='skipped',reason=entry['reason'],revision=task['revision']+1)
                            validate_record('work',after)
                            updates.append({'ref':service._task_path(tid).relative_to(self.vault).as_posix(),
                                'before_sha256':hashlib.sha256(service._task_path(tid).read_bytes()).hexdigest(),'after':after})
                            targets[tid]=fingerprint(task['target_refs']);task_ids.append(tid)
                        audited.append({'slice_id':item['slice_id'],'slice_input_fingerprint':item['input_fingerprint'],
                            'task_ids':item['task_ids'],'target_fingerprints':targets,'reason':entry['reason'],'evidence':proofs})
                        audited[-1].update(known_failure_task_ids=sorted(covered),
                            withheld_unverified_task_ids=sorted(set(item['task_ids'])-covered))
                    report={'contract':'pass-coverage-gaps/v1','request':dict(request),'batch_id':value['batch_id'],
                        'entries':audited,'task_ids':task_ids,'verification_success':False,'domain_outputs_preserved':True}
                    if dry_run:return {'ok':True,'applied':False,'skipped_slices':len(audited),'skipped_tasks':len(task_ids),'coverage_gaps':audited}
                    _write_atomic(journal_path,_json_bytes({'request':dict(request),'report':report,'updates':updates}))
                for update in updates:
                    path=_vault_path(self.vault,update['ref']);data=path.read_bytes();after=_json_bytes(update['after'])
                    if data==after:continue
                    if hashlib.sha256(data).hexdigest()!=update['before_sha256']:fail('STALE_INPUT','skip task changed during recovery')
                    _write_atomic(path,after)
                ref=f'_system/ledgers/ingest-workflows/{wid}/pass-skips/{op}.json';data=_json_bytes(report)
                path=_vault_path(self.vault,ref)
                if path.exists() and path.read_bytes()!=data:fail('IDEMPOTENCY_CONFLICT','skip report differs')
                _write_atomic(path,data)
                value.setdefault('pass_skip_history',[]).append({'operation_id':op,'input_digest':request['input_digest'],
                    'report_ref':ref,'report_sha256':hashlib.sha256(data).hexdigest(),
                    'slice_ids':[e['slice_id'] for e in report['entries']],'task_ids':report['task_ids']})
                value['revision']+=1;self._write(value);journal_path.unlink()
                return {'ok':True,'applied':True,'revision':value['revision'],'report_ref':ref,
                    'skipped_slices':len(report['entries']),'skipped_tasks':len(report['task_ids']),'state':'cancelled','resumed':False}
