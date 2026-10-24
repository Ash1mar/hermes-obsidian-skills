"""Destination contracts across native planning and the real draft consumer."""
import copy
import pytest
from hermes_source_units import ContractError
from hermes_source_units.knowledge_destinations import destination,governed
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json
from test_global_plan_inputs import fixture_value
from test_incremental_global_reduce import page,response,project
from test_p3_knowledge_build import vault,prepare_layered_reduce_batch,resource_reduce_requests
import fixed_reduce
import fixed_global_packets as g
from fixed_destinations import routes,check_links,compile_content


def test_native_topic_path_reaches_governed_draft_without_rewriting_prose(vault):
    service,_,citations,snapshots=prepare_layered_reduce_batch(vault,'destination-routing')
    source_requests=resource_reduce_requests('destination-routing',citations,snapshots)
    reductions=[service.reduce_resource(r)['reduction'] for r in source_requests]
    value=fixture_value();value['backend'].update(batch_id='destination-routing',tasks=snapshots,
        reductions=reductions,identities={'revision':0})
    value['backend']['mapping']={f'c{n}':r['proposals'][0]['candidate_refs'] for n,r in enumerate(source_requests,1)}
    p={**page('c1'),'candidate_refs':['c1','c2'],'path':'设备/泵.md','content':'# Pump X\n\nTransfers coolant; requires filtered fluid.\n'}
    draft=response([p]);before=copy.deepcopy((value,draft))
    request=fixed_reduce.expand(value,draft)
    assert request['runs'][0]['decisions'][0]['path']=='30_Cards/设备/泵.md'
    assert request['runs'][0]['decisions'][0]['content']==p['content']
    result=service.reduce_global(request)
    assert result['ok'] and result['coordination']['run_ids']
    run=_load_json(service._run_path(request['runs'][0]['run_id']))
    assert run['decisions'][0]['path']=='30_Cards/设备/泵.md'
    assert (value,draft)==before


def test_physical_collision_is_rejected_before_any_body_work():
    value=fixture_value();packet=g.next_packet(value,g.units(value),g.initial(),project)
    draft=response([{**page('c1','first'),'path':'requirements/topic.md'},
                    {**page('c2','second'),'path':'30_Cards/requirements/topic.md'}])
    state=g.initial()
    with pytest.raises(ContractError,match='collide after knowledge-root routing'):
        g.accept(value,state,packet,draft)
    assert state==g.initial()


def test_routing_cannot_take_an_unrelated_registered_destination():
    value=fixture_value();old=page('unused','occupied')
    old['path']='30_Cards/topic.md';value['view']['existing_pages']=[old]
    with pytest.raises(ContractError,match='another registered identity'):
        routes(value,[{**page('c1','different'),'path':'topic.md'}])


def test_existing_identity_is_never_moved_by_default_routing():
    value=fixture_value();p=page('c1','existing');key=fingerprint({k:p['identity'][k] for k in ('kind','identity_key')})
    value['backend']['existing_pages'][key]={'subject':{'current_path':p['path']}}
    assert routes(value,[p])[p['path']]==p['path']
    with pytest.raises(ContractError,match='exact registered destination'):
        routes(value,[{**p,'path':'topic.md'}])
    with pytest.raises(ContractError,match='explicit migration'):
        destination('legacy/topic.md',existing=True)


@pytest.mark.parametrize('path',['../outside.md','/outside.md','C:/outside.md','topic.txt','a\\b.md','a/../b.md'])
def test_routing_never_legalizes_unsafe_or_non_markdown_paths(path):
    with pytest.raises(ContractError):destination(path)


def test_allowed_root_and_case_routing_keep_semantic_topic_path():
    assert destination('requirements/topic.md')=='30_Cards/requirements/topic.md'
    assert destination('30_cards/topic.md')=='30_Cards/topic.md'
    for path in ('20_Notes/x.md','30_Cards/x.md','40_Concepts/x.md','50_Projects/x.md'):
        assert destination(path)==path and governed(path)


def test_routing_preserves_relative_sibling_links_but_never_rewrites_source_links():
    value=fixture_value();pages=[{**page('c1','one'),'path':'topic/a.md','content':'[B](b.md)'},
        {**page('c2','two'),'path':'topic/b.md','content':'# B'}]
    before=copy.deepcopy(pages);check_links(pages,routes(value,pages));assert pages==before
    pages[0]['content']='![source](../_system/source.png)'
    with pytest.raises(ContractError,match='authored relative reference'):
        check_links(pages,routes(value,pages))
    assert pages[0]['content']=='![source](../_system/source.png)'
    pages[0]['content']='[[topic/b]]'
    with pytest.raises(ContractError,match='wiki reference'):
        check_links(pages,routes(value,pages))


def test_exact_authorized_asset_target_is_preserved_without_changing_native_body():
    value=fixture_value();asset='_system/sources/artifacts/r/rev/images/a.jpg'
    value['view']['original_evidence']['tasks'][0]['materials'].append({'ref':'m2',
        'asset':{'path':asset,'permission':'this registered whole asset only'}})
    p={**page('c1'),'path':'topic/a.md','content':'# Test\n![original](../'+asset+'#view)\n'}
    before=copy.deepcopy((value,p));destinations=routes(value,[p])
    compiled,edits=compile_content(value,p,destinations)
    assert compiled==p['content'].replace('../_system','../../_system')
    assert len(edits)==1 and edits[0]['canonical_asset_path']==asset
    assert (value,p)==before
    # Another task's registered asset never supplies authority to this page.
    p['candidate_refs']=['c2']
    with pytest.raises(ContractError,match='outside exact authorized originals'):
        compile_content(value,p,routes(value,[p]))
