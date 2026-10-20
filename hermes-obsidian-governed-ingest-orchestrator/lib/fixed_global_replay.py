"""Reconstruct consumed packet boundaries once, never trust a saved plan as proof.

The normal output executor still checks fresh authority, native transcript,
original attempts and the full cumulative validator for every returned packet.
"""
import json,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
import fixed_global_packets as g


def fail(message):raise ContractError('SEMANTIC_OUTPUT_HELD','$',message)


def successful_prefix(adapter,binding,value):
    """Progress is a bounded lookup hint, not accepted state or permission."""
    if not (Path.home()/'.hermes/state.db').is_file():return []
    w=adapter.workflow.status(binding['workflow_id']);allowed={binding['task_id']}
    for pointer in w.get('execution_recovery_history',[]):
        import hashlib
        path=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=pointer['report_sha256']:fail('recovery evidence changed')
        for entry in _load_json(path)['entries']:
            if entry['node']==binding['node']:allowed.add(entry['previous_task_id'])
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs"
    def sequence(p):
        try:return int(p.stem.rsplit('-',1)[1])
        except ValueError:return -1
    paths=sorted(root.glob('global-progress-*.json'),key=sequence,reverse=True)
    if not paths:return []
    content=lambda v:fingerprint({k:v.get(k) for k in ('contract','view','backend')})
    expected=content(value);snapshots={}
    for p in paths:
        row=_load_json(p);b=row.get('binding',{})
        if (row.get('contract')!=g.CONTRACT or b.get('task_id') not in allowed
                or b.get('workflow_id')!=binding['workflow_id'] or b.get('node')!=binding['node']):continue
        ref=row.get('input_ref')
        if not isinstance(ref,str):continue
        path=_vault_path(adapter.workflow.vault,ref)
        if path.parent!=root or not path.is_file():continue
        if ref not in snapshots:snapshots[ref]=content(_load_json(path))
        if snapshots[ref]!=expected:continue
        refs=row.get('successful_call_refs',[])
        if len(refs)!=row.get('completed_packet_count'):fail('prefix call count differs')
        # Every ref is subsequently rebuilt and authenticated, with membership,
        # state and full-wire checks. No serialized state is installed here.
        return refs
    return []


def recorded_packet(adapter,binding,value,remaining,state,ref,project,*,pending=(),home=None):
    from fixed_global_targets import target_view
    from fixed_semantic_recovery import _request_text
    from fixed_semantic_schema import schema_for
    from revision_disposition import native_user
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-calls"
    path=_vault_path(adapter.workflow.vault,ref)
    if path.parent!=root:fail('prefix audit is outside the workflow call directory')
    audit=_load_json(path);phase='global-detail' if pending else 'global-plan'
    if (audit.get('phase')!=phase or audit.get('node')!=binding['node']
            or audit.get('workflow_id')!=binding['workflow_id'] or audit.get('validated') is not True
            or audit.get('template_hash')!=binding['template_hash']):fail('prefix audit identity differs')
    home=Path(home) if home else Path.home()/'.hermes'
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        users=[text for (raw,) in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",(audit['model']['session_id'],))
               if (text:=native_user(raw,audit)) is not None]
    if len(users)!=1:fail('prefix lacks one exact native request')
    recorded=json.loads(_request_text(users[0]));old=recorded['input']
    wanted=old['coordination']['proposal_refs']
    if pending:chosen=[pending[0]]
    else:
        chosen=[];refs=[]
        for unit in remaining:
            chosen.append(unit);refs.extend(p['proposal'] for p in unit['proposals'])
            if refs==wanted:break
            if refs!=wanted[:len(refs)]:fail('recorded packet is not a complete contiguous unit prefix')
        if refs!=wanted or not chosen:fail('recorded packet members differ')
    if sum(len(u['proposals']) for u in chosen)>g.PROPOSAL_OUTPUT_SLOTS:fail('recorded output slots exceed bound')
    view=g.packet_view(value,chosen,state,detail=bool(pending),reader_topics=old.get('page_organization')==g.READER_TOPICS)
    if old.get('output_contract')=='global-target-refs/v1':view=target_view(view)
    if recorded.get('phase')!=phase or view!=old:fail('current packet evidence, directory or preceding state differs')
    sc=schema_for(phase,view)
    if recorded.get('output_schema')!=sc or fingerprint(sc)!=audit['schema_sha256']:fail('recorded output schema differs')
    size=project(phase,view,sc,[])['input_size']
    if size>g.LIMIT:fail('complete recorded packet exceeds current bound')
    return {'view':view,'input_codepoints':size,'unit_count':len(chosen),
        'candidate_refs':sorted({h for u in chosen for h in u['handles']}),
        'proposal_refs':view['coordination']['proposal_refs'],'previous_state_sha256':g.state_digest(state),'units':chosen}


def restore_prefix(adapter,binding,value,refs,project,prove):
    """Rebuild every cumulative state; caller authenticates entry/exit once.

    This installs no saved state and writes no journal, receipt or progress.
    A partial prefix may end while detail units remain; preserve that queue.
    """
    from semantic_output import read_saved_output,check
    state=g.initial();remaining=g.units(value);pending=[];calls=[]
    for ref in refs:
        phase='global-detail' if pending else 'global-plan'
        packet=recorded_packet(adapter,binding,value,remaining,state,ref,project,pending=pending)
        _,call,(updated,requested)=read_saved_output(adapter,binding,phase,packet['view'],
            lambda draft:check(phase,value,packet,state,draft),prove,ref)
        if pending:pending=pending[1:]
        else:remaining=remaining[packet['unit_count']:];pending=requested
        state=updated;calls.append(call)
    return state,remaining,pending,calls
