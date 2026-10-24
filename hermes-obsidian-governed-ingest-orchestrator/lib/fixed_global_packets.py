"""Stable budget accumulation and provisional incremental page coordination.

Native audits bind each packet to the preceding complete planning-state hash.
This module never authors page prose, facts, identities or omission decisions.
"""
import copy,re,unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.runtime_telemetry import measured
from fixed_semantic_schema import schema,schema_for,validate
import fixed_reduce

CONTRACT='proposal-global-plan/v1'
LIMIT=30000
READER_TOPICS='reader-topic/v1'
# Existing 8192-token output cap minus 1024 reserve, at 256 estimated tokens/page.
PROPOSAL_OUTPUT_SLOTS=28
_INDEX=ContextVar('global_packet_lookup',default=None)


@contextmanager
def planning_scope(value):
    """Pure lookups for one immutable input; never cache permission or state."""
    proposals=value['view']['proposals'];by_candidate={}
    for i,p in enumerate(proposals):
        for handle in p['candidate_refs']:by_candidate.setdefault(handle,set()).add(i)
    index={'value':value,'proposals':proposals,'by_candidate':by_candidate}
    token=_INDEX.set(index)
    try:yield
    finally:_INDEX.reset(token)


def fail(code,message):raise ContractError(code,'$',message)


def units(value):
    """Keep a proposal indivisible; shared candidate handles have one owner unit."""
    groups=[]
    for p in value['view']['proposals']:
        handles=set(p['candidate_refs']);hits=[g for g in groups if g['handles']&handles]
        if not hits:groups.append({'proposals':[p],'handles':handles})
        else:
            target=hits[0];target['proposals'].append(p);target['handles'].update(handles)
            for other in hits[1:]:
                target['proposals'].extend(other['proposals']);target['handles'].update(other['handles']);groups.remove(other)
    order={p['proposal']:i for i,p in enumerate(value['view']['proposals'])}
    for g in groups:g['proposals'].sort(key=lambda p:order[p['proposal']])
    groups.sort(key=lambda g:min(order[p['proposal']] for p in g['proposals']))
    for i,g in enumerate(groups,1):g['unit']='u'+str(i)
    covered=[h for g in groups for h in g['handles']]
    if len(covered)!=len(set(covered)) or set(covered)!=set(value['backend']['mapping']):
        fail('INCOMPLETE_COVERAGE','proposal units do not own every global candidate exactly once')
    return groups


def norm(s):return re.sub(r'[^\w]+','',unicodedata.normalize('NFKC',str(s)).casefold())


def names(identity):return [str(identity.get(k,'')) for k in ('identity_key','canonical_name')]+list(identity.get('aliases',[]))


def related(identity,hints):
    left=[norm(n) for n in names(identity) if n]
    right=[norm(n) for h in hints for n in names(h) if n]
    return any(a==b or (min(len(a),len(b))>=6 and (a in b or b in a)) for a in left for b in right)


def state_digest(state):return fingerprint(state)


def heading_anchors(value):
    """Exact same-source section navigation only, never semantic equivalence."""
    index=_INDEX.get()
    if index is not None and index['value'] is value and 'heading_anchors' in index:return index['heading_anchors']
    evidence=value['view'].get('original_evidence',{});tasks={t['task']:t for t in evidence.get('tasks',[])}
    headings=evidence.get('headings',{});sources={}
    for p in value['view'].get('proposals',[]):
        if p.get('source_resource_id'):
            for c in p['candidate_refs']:sources.setdefault(c,set()).add(p['source_resource_id'])
    anchors={}
    for c in value['view']['candidates']:
        found=set()
        for link in c.get('source_evidence',[]):
            for material in tasks.get(link['reading'],{}).get('materials',[]):
                if material['ref'] not in link['support_refs']:continue
                key=material.get('heading');heading=(headings[key] if isinstance(headings,list) and isinstance(key,int) and 0<=key<len(headings)
                    else headings.get(str(key)) if isinstance(headings,dict) else None)
                if isinstance(heading,list) and len(heading)>=2 and all(isinstance(h,str) and h.strip() for h in heading):
                    found.update((source,tuple(heading)) for source in sources.get(c['candidate'],()))
        anchors[c['candidate']]=found
    if index is not None and index['value'] is value:index['heading_anchors']=anchors
    return anchors


def packet_view(value,chosen,state,*,detail=False,reader_topics=False):
    handles={h for g in chosen for h in g['handles']}
    proposals=[{**p,'_detail_unit':g['unit']} for g in chosen for p in g['proposals']]
    hints=[h for p in proposals for h in p['identity_hints']]
    # Selection is program-side; candidate names are not default planning input.
    hints += [{'canonical_name':c['name']} for c in value['view']['candidates'] if c['candidate'] in handles and c.get('name')]
    anchors=heading_anchors(value) if reader_topics else {}
    selected_anchors=set().union(*(anchors.get(h,set()) for h in handles)) if reader_topics else set()
    existing=[p for p in value['view'].get('existing_pages',[]) if related(p['identity'],hints)]
    planned=[]
    index=_INDEX.get()
    for page in state['pages']:
        if related(page['identity'],hints) or (reader_topics and any(selected_anchors&anchors.get(h,set()) for h in page['candidate_refs'])):
            if index is not None and index['value'] is value:
                hits=set().union(*(index['by_candidate'].get(h,set()) for h in page['candidate_refs']))
                topics=[index['proposals'][i] for i in sorted(hits)]
            else:
                handles_for_page=set(page['candidate_refs'])
                topics=[p for p in value['view']['proposals'] if handles_for_page.intersection(p['candidate_refs'])]
            planned.append({'identity':copy.deepcopy(page['identity']),'path':page['path'],
                'topics':list(dict.fromkeys(p['summary'] for p in topics)),
                'source_paths':sorted({p['source_path'] for p in topics if p.get('source_path')})})
    sliced={'view':{**value['view'],'candidates':[c for c in value['view']['candidates'] if c['candidate'] in handles],
                    'proposals':proposals,'existing_pages':existing}}
    view=(fixed_reduce.global_detail_view if detail else fixed_reduce.global_plan_view)(sliced)
    view['phase']='global-detail' if detail else 'global-plan'
    view['coordination']={'contract':CONTRACT,'previous_state_sha256':state_digest(state),
        'proposal_refs':[p['proposal'] for p in proposals],'planned_pages':planned,
        'rule':'Default to whole proposal_refs; the program owns and expands candidate membership. '
               'Request detail_unit_refs only for a concrete unresolved split or conflict, naming the reason. '
               'Do not request raw facts for re-review or repeat prior candidates. '
               'Append only to an exact offered planned identity/path; preserve version/scope differences or keep separate. '
               'Full conditions, QA and original evidence are checked by per-page writing, not approved by this routing.'}
    if detail:
        view['coordination']['rule']='Route every candidate in this requested detail unit once, without omission. Resolve only the stated local grouping ambiguity, preserve scope/version/QA limits, and append only to exact offered identities and paths. Full original evidence is read during page writing.'
        view['coordination']['detail_reason']=chosen[0]['detail_reason']
    if reader_topics:
        view['page_organization']=READER_TOPICS
        view['coordination']['rule']+=' Same-source section matches add comparison candidates only; shared headings do not prove equivalence, applicability, facts or completeness. Choose independently readable topics, not one page per proposal, time interval or diagram fragment. Keep compatible conditions as explicit sections; preserve distinct versions, scope, exceptions, conflicts and QA. Never force a merge to reduce page count.'
    return view


@measured('global_packet_pack')
def next_packet(value,remaining,state,project,*,view_transform=None,reader_topics=False):
    chosen=[];prepared=None
    for group in remaining:
        count=sum(len(g['proposals']) for g in [*chosen,group])
        if count>PROPOSAL_OUTPUT_SLOTS:
            if not chosen:fail('READING_WINDOW_OVERSIZE','one indivisible proposal unit exceeds the fixed output slots')
            break
        view=packet_view(value,[*chosen,group],state,reader_topics=reader_topics)
        if view_transform is not None:view=view_transform(view)
        projected=project('global-plan',view,schema_for('global-plan',view),[])
        if projected['input_size']>LIMIT:
            if not chosen:fail('READING_WINDOW_OVERSIZE','one complete proposal unit plus related page boundaries exceeds global planning budget')
            break
        chosen.append(group);prepared=(view,projected['input_size'])
    if not chosen:fail('INCOMPLETE_COVERAGE','incremental global planning has no remaining unit')
    view,size=prepared
    return {'view':view,'input_codepoints':size,'unit_count':len(chosen),
            'candidate_refs':sorted({h for g in chosen for h in g['handles']}),
            'proposal_refs':view['coordination']['proposal_refs'],'previous_state_sha256':state_digest(state),'units':chosen}


def accept(value,state,packet,draft):
    """Validate packet decisions and all accumulated identity/path/coverage invariants."""
    from fixed_global_targets import expand
    draft=expand('global-detail',packet['view'],draft)
    validate('global-detail',draft)
    if 'blocked_reason' in draft:fail('SEMANTIC_REVIEW_REQUIRED',draft['blocked_reason'])
    if state_digest(state)!=packet['previous_state_sha256']:fail('STALE_INPUT','preceding global plan state changed')
    used=[c for p in draft['pages'] for c in p['candidate_refs']]+draft['omitted_candidate_refs']
    if len(used)!=len(set(used)):fail('DUPLICATE','global packet repeats or also omits a candidate')
    if set(used)!=set(packet['candidate_refs']):fail('INCOMPLETE_COVERAGE','global packet must decide only and all its assigned candidates')
    result=copy.deepcopy(state)
    allowed={fingerprint(p['identity']):p for p in packet['view']['coordination']['planned_pages']}
    old_allowed={fingerprint({'kind':p['identity']['kind'],'identity_key':p['identity']['identity_key']}):p
                 for p in packet['view'].get('existing_pages',[])}
    key=lambda p:fingerprint({k:p['identity'][k] for k in ('kind','identity_key')})
    for page in draft['pages']:
        identity_key=key(page);found=next((p for p in result['pages'] if key(p)==identity_key),None)
        existing=value['backend']['existing_pages'].get(identity_key)
        if existing:
            if identity_key not in old_allowed:fail('UNRESOLVED_REFERENCE','update target was outside this packet related identity directory')
            parent=old_allowed[identity_key]
            if page['path']!=parent['path']:fail('IDENTITY_CONFLICT','existing target path differs')
        if found:
            if fingerprint(page['identity']) not in allowed or found['identity']!=page['identity'] or found['path']!=page['path']:
                fail('IDENTITY_CONFLICT','cross-packet append must name an exact offered planned identity and path')
            found['candidate_refs'].extend(page['candidate_refs'])
        else:result['pages'].append(copy.deepcopy(page))
    paths=[p['path'].casefold() for p in result['pages']]
    if len(paths)!=len(set(paths)):fail('IDENTITY_CONFLICT','planned page paths collide')
    # A new identity may not collide with an unrelated registered page path.
    registered={p['path'].casefold():key(p) for p in value['view'].get('existing_pages',[])}
    if any(p['path'].casefold() in registered and registered[p['path'].casefold()]!=key(p) for p in result['pages']):
        fail('IDENTITY_CONFLICT','planned path belongs to another registered identity')
    from fixed_destinations import routes
    routes(value,result['pages'])  # Validate storage roots and physical collisions before body work.
    result['omitted_candidate_refs'].extend(draft['omitted_candidate_refs'])
    all_handles=[c for p in result['pages'] for c in p['candidate_refs']]+result['omitted_candidate_refs']
    if len(all_handles)!=len(set(all_handles)):fail('DUPLICATE','accumulated plan uses a candidate twice')
    result['proposal_refs'].extend(packet['proposal_refs'])
    result['reasons'].append(draft['reason'])
    return result


def accept_proposals(value,state,packet,draft):
    """Native routing names pN; only the program expands pN to full member sets."""
    from fixed_global_targets import expand
    draft=expand('global-plan',packet['view'],draft)
    validate('global-plan',draft)
    if 'blocked_reason' in draft:fail('SEMANTIC_REVIEW_REQUIRED',draft['blocked_reason'])
    queries=draft['detail_unit_refs'];by_unit={g['unit']:g for g in packet['units']}
    if not set(queries)<=set(by_unit):fail('UNRESOLVED_REFERENCE','detail request is outside this packet')
    pending=[{**g,'detail_reason':draft['reason']} for g in packet['units'] if g['unit'] in queries]
    if packet['view'].get('page_organization')==READER_TOPICS:
        for unit in pending:unit['reader_topics']=True
    deferred={p['proposal'] for g in pending for p in g['proposals']}
    assigned=[r for p in draft['pages'] for r in p['proposal_refs']]
    if len(assigned)!=len(set(assigned)) or set(assigned)&deferred:
        fail('DUPLICATE','a proposal was assigned twice or also requested as detail')
    if set(assigned)|deferred!=set(packet['proposal_refs']):
        fail('INCOMPLETE_COVERAGE','route each offered proposal or request its complete detail unit')
    lookup={p['proposal']:p for g in packet['units'] for p in g['proposals']}
    pages=[]
    for page in draft['pages']:
        refs=list(dict.fromkeys(c for r in page['proposal_refs'] for c in lookup[r]['candidate_refs']))
        pages.append({'identity':page['identity'],'path':page['path'],'candidate_refs':refs})
    normal_handles={c for r in assigned for c in lookup[r]['candidate_refs']}
    # This internal candidate expansion already has canonical identities.
    part={**packet,'view':{k:v for k,v in packet['view'].items() if k!='output_contract'},
          'proposal_refs':assigned,'candidate_refs':sorted(normal_handles)}
    result=accept(value,state,part,{'pages':pages,'omitted_candidate_refs':[],'reason':draft['reason']})
    return result,pending


def detail_packet(value,unit,state,project):
    view=packet_view(value,[unit],state,detail=True,reader_topics=unit.get('reader_topics',False))
    size=project('global-detail',view,schema('global-detail'),[])['input_size']
    if size>LIMIT:fail('READING_WINDOW_OVERSIZE','one requested complete detail unit exceeds the bound; no truncation')
    return {'view':view,'input_codepoints':size,'unit_count':1,'units':[unit],
            'candidate_refs':sorted(unit['handles']),'proposal_refs':[p['proposal'] for p in unit['proposals']],
            'previous_state_sha256':state_digest(state)}


def initial():return {'pages':[],'omitted_candidate_refs':[],'proposal_refs':[],'reasons':[]}


def complete(value,state):
    if set(state['proposal_refs'])!={p['proposal'] for p in value['view']['proposals']} or len(state['proposal_refs'])!=len(set(state['proposal_refs'])):
        fail('INCOMPLETE_COVERAGE','incremental proposal accounting differs')
    draft={'pages':copy.deepcopy(state['pages']),'omitted_candidate_refs':state['omitted_candidate_refs'],
           'reason':'Incremental native packet coordination; '+ '; '.join(state['reasons'])}
    fixed_reduce.expand(value,{**draft,'pages':[{**p,'content':'planning-only'} for p in draft['pages']]})
    return draft
