"""Native tools; workers own semantics, supported adapters own mechanics."""
import json
import os
from pathlib import Path
import subprocess
import sys


def register(ctx):
    root = Path(ctx.get_config('skills_root',str(Path(__file__).resolve().parents[2]/'skills/domain')))
    ingest, orchestrator = root/'hermes-obsidian-controlled-ingest', root/'hermes-obsidian-governed-ingest-orchestrator'
    sys.path.insert(0,str(ingest/'lib'))
    from hermes_source_units.semantic_submission import submission_schema
    from hermes_source_units.validation import _check_shape
    bridge = orchestrator/'scripts/native_ingest.py'
    vault_property = {'type':'string','minLength':1}
    simple = {'type':'object','properties':{'vault':vault_property},'required':['vault'],'additionalProperties':False}

    def worker_available():
        return bool(os.environ.get('HERMES_KANBAN_TASK') and os.environ.get('HERMES_KANBAN_WORKSPACE') and bridge.is_file())

    def operator_available():
        return bool(not os.environ.get('HERMES_KANBAN_TASK') and not os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') and bridge.is_file())

    def semantic_schema(confirmation=False):
        schema = submission_schema(confirmation=confirmation)
        schema['properties']['vault'] = vault_property
        schema['required'].append('vault')
        return schema

    status_schema = {'type':'object','properties':{'vault':vault_property,
        'workflow_id':{'type':'string','minLength':1},'runtime':{'type':'boolean'}},
        'required':['vault'],'additionalProperties':False}
    control_schema = {'type':'object','properties':{'vault':vault_property,
        'workflow_id':{'type':'string','minLength':1},'action':{'type':'string','enum':['resume','cancel']},
        'operation_id':{'type':'string','pattern':'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$'}},
        'required':['vault','workflow_id','action','operation_id'],'additionalProperties':False}

    def invoke(args,action,schema,check):
        try:
            if not check():
                raise ValueError('tool unavailable in this operator/worker context')
            _check_shape(schema,args,{},'$')
            result = subprocess.run([sys.executable,str(bridge),'--action',action],
                input=json.dumps(args,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=600)
            return result.stdout.strip() or result.stderr.strip()
        except (ValueError,OSError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
            result = {'ok':False,'error':str(exc)}
            if hasattr(exc,'code'):
                result.update(code=exc.code,path=exc.path)
            return json.dumps(result,ensure_ascii=False)

    tools = (
        ('ingest_begin_pass','begin',simple,worker_available,'Begin this native bounded Pass and read all authorized material in one call. Saves checked binding and presents shared text once; no manual JSON expansion or canonical-package reading.'),
        ('ingest_read_pass_input','read',simple,worker_available,'Re-read this bound input with live identity checks, only when necessary. Never expands the window.'),
        ('ingest_submit_passes','submit',semantic_schema(),worker_available,'Submit actual candidate or authored citation semantics. conditions/exceptions/support_refs are arrays. Fused preflight/persistence; receipts name pass_kind and next_action. A written citation completes the task; do not confirm it again.'),
        ('ingest_confirm_citations','confirm',semantic_schema(True),worker_available,'After actual review of the original sequence-0 candidate against authorized evidence and QA, confirm unchanged using its exact candidate_pass_id and actual review_note. For changes author the citation with ingest_submit_passes; it needs no subsequent confirmation.'),
        ('ingest_pass_heartbeat','heartbeat',simple,worker_available,'Renew the current lease and save binding; submissions also renew. Use during long reading; never revives a stopped/expired lease.'),
        ('ingest_complete_pass','complete',simple,worker_available,'Verify all required durable Passes and complete this slice. Returns audited counts and native reconciliation status. Then end the worker; no extra statistics, source rereads or repeated completion.'),
        ('ingest_workflow_status','status',status_schema,operator_available,'Read compact authoritative workflow/slice/Pass counts. Omit workflow_id only for a unique existing workflow. runtime=true adds one native task-list summary; no per-card logs or domain work.'),
        ('ingest_control_workflow','control',control_schema,operator_available,'Only for user-authorized resume/cancel of an existing workflow. Builds current actor/revision/digest and calls supported dispatch; stable operation_id retains request/receipt for replay. Resume never releases pauses; no reset, replacement or semantic work.'),
    )
    for name,action,schema,check,description in tools:
        def handler(args,_action=action,_schema=schema,_check=check,**kw):
            return invoke(args,_action,_schema,_check)
        ctx.register_tool(name=name,toolset='ingest_workflow' if check is operator_available else 'ingest_pass',
            schema={'name':name,'description':description,'parameters':schema},handler=handler,check_fn=check)
