"""Authenticate old bodies without migrating budgets, then measure future wires."""
import copy,json
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint

def load(vault,workflow_id):
    from ingest_kanban import IngestKanbanAdapter
    from page_budget_store import operation,Snapshot
    from fixed_semantic_recovery import _request_text,native_reply,proof_scope
    from fixed_page_evidence import restore_pair
    from independent_body import restore
    from hermes_source_units.source_units import _load_json
    adapter=IngestKanbanAdapter(vault);root=Path(vault)/f'_system/ledgers/ingest-workflows/{workflow_id}'
    w=adapter.workflow.status(workflow_id);card=next(c for c in w['kanban']['task_map'] if c['node']=='global-reduce')
    binding=_load_json(root/f'bindings/{fingerprint(card["idempotency_key"])}.json');items={};audits=[]
    with operation(),proof_scope():
        scan=Snapshot(root)
        for rid in scan.index['requests']:
            q=scan.request(rid)
            if 'response' not in q:raise ContractError('SEMANTIC_OUTPUT_HELD','$','saved request unknown; comparison cannot assume free or successful')
            wire=json.loads(_request_text(q['wire']['user']))['input']
            if wire.get('evidence_encoding')=='lossless-page-pair/v1':wire=restore_pair(wire)
            independent=wire.get('body_call_contract')=='independent-page-body/v1'
            if independent:wire=restore(wire)
            from fixed_semantic_media import image_catalog,select_images
            pictures=[]
            if independent:
                from review_pair_request import images_for
                pictures,_=images_for(adapter,wire)
            elif q.get('image_inputs'):
                e=wire['original_evidence'];pictures=select_images(e['tasks'],image_catalog(adapter,e))
            response=q['response'];proof=native_reply(adapter,binding,Path(vault)/response['audit_ref'],q['wire'],q['schema'],pictures)
            if not proof or proof[0]!=response['raw']:raise ContractError('STALE_INPUT','$','saved body input lacks exact native completion')
            audits.append(response['audit_ref'])
            for slot,m in q['members'].items():
                v=wire['pages'][slot] if independent else {**wire['pages'][slot],'original_evidence':wire['original_evidence']}
                row=scan.row(m['page_key'])
                if fingerprint(v)!=row['input_sha256']:raise ContractError('STALE_INPUT','$','body evidence differs from original reservation')
                p={'identity':v['identity'],'path':v['path'],'candidate_refs':[c['candidate'] for c in v['candidates']]}
                items[m['page_key']]=(p,{k:x for k,x in v.items() if k!='evidence_encoding'})
    return sorted(items.values(),key=lambda p:p[0]['path']),adapter,audits

def compare(items,adapter,profile_name):
    from independent_body import CONTRACT,prepare
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_media import image_catalog,select_images
    from page_pair_eligibility import pair
    from input_budget import profile
    from request_cost import rank
    budget=profile(profile_name);pending=copy.deepcopy(items);groups=[];held=[];splits=[]
    for _,v in pending:v['body_call_contract']=CONTRACT
    def prepared(group):
        images=[]
        if len(group)==1:
            e=group[0][1]['original_evidence'];images=select_images(e['tasks'],image_catalog(adapter,e))
        return prepare(group,prepare_request_projection,images,budget=budget,limit=budget['phases']['page-pair']['input_codepoints'])
    while pending:
        first=pending.pop(0);group=[first];packed=None
        if pending:
            qualification=pair(first,pending[0])
            if qualification['eligible']:
                trial=prepared([first,pending[0]])
                singles=[prepared([p])[2] for p in (first,pending[0])]
                metric='text_tokens' if all(p['request_cost'].get('text_tokens') is not None for p in [trial[2],*singles]) else 'utf8_bytes'
                saving=sum(p['request_cost'][metric] for p in singles)-trial[2]['request_cost'][metric]
                if trial[2]['budget_check']['fits'] and saving>0:group.append(pending.pop(0));packed=trial
                else:splits.append({'pages':[first[0]['path'],pending[0][0]['path']],'reasons':trial[2]['budget_check']['split_reasons']+(['no_representation_saving'] if saving<=0 else [])})
            else:splits.append({'pages':[first[0]['path'],pending[0][0]['path']],'reasons':qualification['reasons'],'required_missing':[r['required_missing'] for r in qualification['pages']]})
        packed=packed or prepared(group);v,sc,p,variants=packed
        row={'pages':[x[0]['path'] for x in group],**p['budget_check'],'request_cost':p['request_cost'],
            'estimated_output_tokens':p['estimated_body_output_tokens'],'input_size':p['input_size'],
            'variants':[{'encoding':v.get('evidence_encoding','original'),'input_size':x['input_size'],'request_cost':x['request_cost']} for v,x in variants]}
        (groups if p['budget_check']['fits'] else held).append(row)
    return {'profile':profile_name,'same_planned_pages':len(items),'estimated_requests':len(groups),'pairs':sum(len(g['pages'])==2 for g in groups),
        'groups':groups,'held':held,'split_decisions':splits,'same_candidate_assignments':True,'actual_new_model_calls':0,
        'actual_token_usage':None,'actual_output_truncations':None,'actual_valid_bodies':None,'actual_additional_attempts':None,'model_wait_seconds':None,
        'note':'Mechanical fixed-plan comparison. Output estimates are heuristics; true result quality and wait remain unverified.'}
