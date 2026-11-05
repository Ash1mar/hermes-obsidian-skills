"""Restore a returned native source response lost at audit serialization, zero calls.

Admission seals source, finite ordinal, ended native owner, exact transcript and
one successful SDK span. Only a newly authorized worker writes the audit link;
ordinary native proof and domain validation still decide the output.
"""
from pathlib import Path
import json,sqlite3,hashlib,copy
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint

CONTRACT='returned-native-output/v1'
def fail(msg):raise ContractError('RECOVERY_NOT_ALLOWED','$',msg)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def contract_hash():
 return fingerprint({n:sha(Path(__file__).parent/n) for n in ('returned_native_output.py','fixed_semantic.py','native_stream_policy.py','reduce_execution_recovery.py','fixed_semantic_recovery.py','semantic_output.py','hermes_source_units/execution_plan.py')})

def verify(adapter,w,node,binding,entry,failure,runs):
 from source_unknown_recovery import identity
 from fixed_reduce import preview_snapshot
 from fixed_reduce_packets import plan
 from fixed_semantic import prepare_request_projection,decode_semantic_response
 from fixed_semantic_schema import schema_for
 from fixed_semantic_media import image_catalog,select_images
 from source_result_review import review_view
 from semantic_output import path_for
 from revision_disposition import native_user
 from hermes_source_units.runtime_telemetry import runtime_directory
 if node.kind!='resource-reduce' or failure.get('error_code')!='INVALID_CANONICAL_VALUE':fail('only ended source audit serialization failure is eligible')
 root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}"
 sp=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);old=_load_json(sp)
 if sp.parent!=root/'semantic-inputs' or sha(sp)!=entry['snapshot_sha256'] or old['binding']!=binding or identity(old)!=identity(preview_snapshot(adapter,binding)):fail('original complete source snapshot changed')
 offered={}
 for packet in plan(adapter,old,prepare_request_projection):
  view=review_view(old,packet['view']);offered[path_for(adapter,binding,'resource-reduce',view).name]=view
 events=[]
 for p in runtime_directory(adapter.workflow.vault,w['workflow_id']).glob('*.jsonl'):
  for line in p.read_text().splitlines():
   try:e=json.loads(line)
   except ValueError:continue
   if e.get('task_id')==binding['task_id'] and e.get('workflow_id')==w['workflow_id']:events.append(e)
 seals=[];ended={str(r['id']) for r in runs}
 with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
  for jp in (root/'output-attempts').glob('*.json'):
   j=_load_json(jp)
   for index,a in enumerate(j.get('attempts',[]),1):
    if a.get('task_id')!=binding['task_id'] or a.get('audit_ref'):continue
    if (jp.name not in offered or a.get('call_outcome') or a.get('state')!='rejected' or a.get('diagnostic',{}).get('code')!='INVALID_CANONICAL_VALUE'
       or a.get('attempt')!=index or a.get('attempt_id')!=fingerprint({'path':jp.name,'attempt':index}) or str(a.get('run_id')) not in ended):fail('unaccounted or unknown reservation; no returned-output repair')
    view=offered[jp.name];sc=schema_for('resource-reduce',view)
    images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
    prepared=prepare_request_projection('resource-reduce',view,sc,images,a.get('correction'))
    if j['input_sha256']!=fingerprint(view) or j['schema_sha256']!=fingerprint(sc) or a['wire_sha256']!=fingerprint(prepared['wire']):fail('original full request differs')
    sid=a.get('session_id')
    if not sid or db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):fail('not a native session')
    rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(sid,)))
    if any(t and t!='[]' for _,_,t in rows):fail('native session used tools')
    users=[c for r,c,_ in rows if r=='user'];answers=[c for r,c,_ in rows if r=='assistant']
    image_inputs=[{k:v for k,v in i.items() if k!='url'} for i in images]
    if len(users)!=1 or len(answers)!=1 or native_user(users[0],{'user_sha256':fingerprint(prepared['wire']['user']),'image_inputs':image_inputs})!=prepared['wire']['user']:fail('exact native transcript differs')
    if any(c!=prepared['wire']['system'] for r,c,_ in rows if r=='system'):fail('native system prompt differs')
    raw=answers[0];decode_semantic_response(raw)
    related=[e for e in events if e.get('session_id')==sid and e.get('phase')=='resource-reduce' and str(e.get('native_run_id'))==str(a['run_id'])]
    starts=[e for e in related if e['name']=='sdk_request_start'];ends=[e for e in related if e['name']=='sdk_model_request']
    if len(starts)!=1 or len(ends)!=1 or ends[0].get('error') or not starts[0].get('request_id') or starts[0]['request_id']!=ends[0].get('request_id'):fail('one successful SDK return must be proven')
    audit={'contract':'hermes-fixed-semantic-call/v1','workflow_id':w['workflow_id'],'node':node.name,'native_task_id':binding['task_id'],
      'native_run_id':a['run_id'],'template_hash':binding['template_hash'],'phase':'resource-reduce','input_sha256':fingerprint(prepared['wire']),
      'user_sha256':fingerprint(prepared['wire']['user']),'schema_sha256':fingerprint(sc),'response_sha256':fingerprint(raw),
      'model':{'origin':'hermes_native_model','session_id':sid,'response_sha256':fingerprint(raw),'tools':0,'restored_from_ended_native_transcript':True},
      'receipt_refs':[],'output_attempt_id':a['attempt_id'],'request_id':starts[0]['request_id'],'output_codepoints':len(raw),'output_utf8_bytes':len(raw.encode()),
      'image_inputs':image_inputs,'recovery_contract':CONTRACT,'transport_timing':'original SDK trace; missing call metadata remains unavailable'}
    audit.update(input_codepoints=prepared['input_size'],static_codepoints=prepared['static_size'],limit=30000,wire_contract='semantic-input/v1',call_category='restored_native_result')
    spans=[e for e in events if e.get('name')=='model_call' and e.get('request_id')==starts[0]['request_id'] and not e.get('error')]
    if len(spans)==1:audit['timeline_span_id']=spans[0]['span_id']
    seals.append({'journal_name':jp.name,'journal_sha256':sha(jp),'input_sha256':j['input_sha256'],'original_attempt':copy.deepcopy(a),
       'audit':audit,'response_sha256':fingerprint(raw),'native_return_span_id':ends[0]['span_id']})
 if not seals:fail('no unlinked returned response to restore')
 return {'returned_native_output':{'contract':CONTRACT,'source_identity':identity(old),'snapshot_ref':entry['snapshot_ref'],
  'snapshot_sha256':entry['snapshot_sha256'],'outputs':seals,'new_model_calls_granted':0,'original_attempts_preserved':True,'unknown_completion_allowed':False}}

def restore_packet(adapter,binding,value,packet):
 from source_unknown_recovery import identity
 from source_result_review import review_view
 from semantic_output import path_for
 from fixed_history import entries
 from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
 view=review_view(value,packet['view']);jp=path_for(adapter,binding,'resource-reduce',view)
 for pointer,entry in entries(adapter,binding,scopes=('reduce-cancel/v1',)):
  proof=entry.get('returned_native_output')
  if not proof:continue
  if proof['contract']!=CONTRACT or proof['source_identity']!=identity(value) or proof['new_model_calls_granted']!=0:fail('restored output source authority changed')
  if sha(_vault_path(adapter.workflow.vault,proof['snapshot_ref']))!=proof['snapshot_sha256']:fail('original returned-output snapshot changed')
  for row in proof['outputs']:
   if row['journal_name']!=jp.name:continue
   audit=row['audit'];ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-calls/{fingerprint(audit)}.json";ap=_vault_path(adapter.workflow.vault,ref)
   journal=_load_json(jp);original=row['original_attempt'];current=journal['attempts'][original['attempt']-1]
   mutable=('audit_ref','state','correctable','diagnostic','issue_classification')
   if (journal['input_sha256']!=row['input_sha256'] or any(current.get(k)!=v for k,v in original.items() if k not in mutable)
      or (sha(jp)!=row['journal_sha256'] and current.get('audit_ref')!=ref)):fail('returned-output original budget changed')
   adapter.worker_check(binding)
   with worker_binding(binding),workflow_write_guard(adapter.workflow.vault,kinds=('resource-reduce',),actor=binding['actor']):
    if ap.exists():
     existing=_load_json(ap)
     if {k:v for k,v in existing.items() if k not in ('receipt_refs','validated')}!={k:v for k,v in audit.items() if k not in ('receipt_refs','validated')}:fail('restored audit changed')
    else:_write_atomic(ap,_json_bytes(audit))
   adapter.worker_check(binding)
