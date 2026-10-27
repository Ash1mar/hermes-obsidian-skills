"""Scoped output references, with exact legacy journal and attempt preservation."""
import copy
from hermes_source_units import ContractError
from hermes_source_units.validation import _check_shape
from fixed_semantic_schema import schema_for

CONTRACT='global-target-refs/v1'


def offered(view):
    return [*view.get('existing_pages',[]),*view.get('coordination',{}).get('planned_pages',[])]


def target_view(view):
    result=copy.deepcopy(view);result['output_contract']=CONTRACT
    for i,target in enumerate(offered(result),1):target['target_ref']='t'+str(i)
    result['coordination']['rule']+=' For an offered existing/planned page, return its target_ref only, without copying identity, aliases or path. For a NEW page return identity and path. A target_ref selects the exact offered target; relevance is not semantic equivalence. Preserve source/version/scope differences.'
    return result


def has_legacy_history(adapter,binding,phase,view):
    """Seeds count as consumed history before a journal is materialized."""
    from semantic_output import path_for
    old=path_for(adapter,binding,phase,view)
    has_old=old.exists()
    if not has_old:
        # Explicit legacy seeds are also consumed history, even before materialization.
        from fixed_history import entries
        for _,entry in entries(adapter,binding,select=lambda e:e.get('output_seed')
                if e.get('output_seed',{}).get('journal_name')==old.name else None,
                query=('legacy_seed',old.name)):
            if entry:
                has_old=True;break
    return has_old


def select(adapter,binding,phase,packet,project):
    """A consumed old request always retains its original view/schema/allowance."""
    from semantic_output import path_for
    legacy=packet['view'];has_old=has_legacy_history(adapter,binding,phase,legacy)
    short=target_view(legacy)
    if has_old:
        if path_for(adapter,binding,phase,short).exists():
            raise ContractError('SEMANTIC_OUTPUT_HELD','$','both routing contracts have consumed history; no allowance reset')
        return packet
    size=project(phase,short,schema_for(phase,short),[])['input_size']
    if size>30000:
        raise ContractError('READING_WINDOW_OVERSIZE','$','complete short-target routing exceeds bound; no truncation or call')
    return {**packet,'view':short,'input_codepoints':size}


def planning_packet(adapter,binding,value,remaining,state,project):
    """Replay old boundaries exactly; pack new work against its actual full wire."""
    from semantic_output import path_for
    from fixed_global_packets import next_packet
    legacy=next_packet(value,remaining,state,project)
    if (has_legacy_history(adapter,binding,'global-plan',legacy['view'])
            or path_for(adapter,binding,'global-plan',target_view(legacy['view'])).exists()):
        return select(adapter,binding,'global-plan',legacy,project)
    # No consumed legacy boundary can be replaced by this smaller new packet.
    # The transform runs before every trial measurement, including schema/rules.
    short=next_packet(value,remaining,state,project,view_transform=target_view)
    if has_legacy_history(adapter,binding,'global-plan',short['view']):return short
    # Consumed legacy/short wires above remain exact. Only untouched work gets
    # section navigation and reader-topic guidance, with full trial measurement.
    return next_packet(value,remaining,state,project,view_transform=target_view,reader_topics=True)


def expand(phase,view,draft):
    if view.get('output_contract')!=CONTRACT:return draft
    _check_shape(schema_for(phase,view),draft,{},'$')
    if 'blocked_reason' in draft:return draft
    targets={t['target_ref']:t for t in offered(view)};result=copy.deepcopy(draft)
    for page in result['pages']:
        ref=page.pop('target_ref',None)
        if ref is None:continue
        if ref not in targets:raise ContractError('UNRESOLVED_REFERENCE','$','target_ref is outside this packet directory: '+ref)
        target=targets[ref];page.update(identity=copy.deepcopy(target['identity']),path=target['path'])
    return result
