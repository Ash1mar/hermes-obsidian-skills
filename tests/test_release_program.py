import copy
from pathlib import Path
import pytest
from test_delivery_scope import scoped
from test_partial_finalize import ready,vault,writer
from hermes_source_units.delivery_scope import close,closure_refs
from hermes_source_units import FileVaultFinalizeService,ContractError
from hermes_source_units.release_lint import classify

def test_scoped_navigation_retains_labels_but_rejects_actual_ambiguous_links(tmp_path):
    service=object.__new__(FileVaultFinalizeService);service.vault=tmp_path
    subjects=[{'page_id':str(i),'subject_id':str(i),'current_path':str(i)+'.md','canonical_name':str(i),'aliases':['Shared']} for i in range(2)]
    for s in subjects:(tmp_path/s['current_path']).write_text('independent')
    assert service._navigation(subjects)[1]
    assert service._navigation(subjects,scoped=True)[1]==[]
    (tmp_path/'0.md').write_text('[[Shared]]')
    assert any(b.startswith('ambiguous-link:') for b in service._navigation(subjects,scoped=True)[1])

def test_lint_only_proven_excluded_local_errors_are_nonblocking():
    plan={'subjects':[{'current_path':'in.md'}],'delivery':{'excluded':[{'path':'out.md'}]}}
    lint={'ok':False,'issues':[{'code':'evidence.unknown_section','severity':'error','path':'out.md'}]}
    result=classify(plan,lint)
    assert result['ok'] and not result['whole_vault_ok'] and lint['ok'] is False
    for code,path in [('unknown.rule','out.md'),('evidence.unknown_section','in.md'),('governance.invalid','out.md')]:
        assert not classify(plan,{'ok':False,'issues':[{'code':code,'path':path,'severity':'error'}]})['ok']

def test_excluded_canonical_body_drift_does_not_poison_selected_release(scoped,vault):
    k,run,b,basis,pointer,req=scoped
    with writer(k):close(k,b,basis)
    service=FileVaultFinalizeService(vault)
    request={'release_id':'scope-local','actor':'agent','expected_state_revision':0,'build_run_ids':[run['run_id']],
             'delivery_scopes':closure_refs(k,[run['run_id']])}
    from hermes_source_units.scoped_release import build_plan
    from hermes_source_units.delivery_scope import release_closures
    scopes=release_closures(k,request['delivery_scopes'],[run['run_id']])
    expected=build_plan(service,request,service._state(),[run],scopes)
    # Add an unrelated active identity using an existing immutable revision.
    registry=k._registry();extra=copy.deepcopy(registry['subjects'][0]);extra.update(page_id='a'*64,subject_id='b'*64,current_path='unrelated.md',canonical_name='Unrelated',identity_key='unrelated',aliases=[])
    registry['subjects'].append(extra)
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    _write_atomic(vault/'_system/metadata/knowledge-identities.json',_json_bytes(registry))
    (vault/'unrelated.md').write_text('uncommitted unrelated content')
    plan=build_plan(service,request,service._state(),[run],scopes)
    assert plan['subjects']==expected['subjects'] and not plan['blockers']
    assert any(e['page_id']=='a'*64 for e in plan['delivery']['excluded'])
    (vault/run['page_revisions'][0]['path']).write_text('changed selected evidence')
    with pytest.raises(ContractError):build_plan(service,request,service._state(),[run],scopes)


