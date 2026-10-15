"""Sequential source Reduce packets, keeping each candidate's complete evidence."""
import copy
import json
import hashlib
from pathlib import Path

from hermes_source_units import ContractError
from fixed_semantic_schema import schema, validate
from fixed_semantic_media import image_catalog, select_images, images_fit, image_usage
from fixed_reduce import resource_view

CONTRACT='source-reduce-packets/v1'


def contract_hash():
    root=Path(__file__).resolve().parent
    return hashlib.sha256(b''.join((root/name).read_bytes() for name in
        ('fixed_reduce_packets.py','fixed_reduce.py','fixed_evidence.py','fixed_semantic.py'))).hexdigest()


def plan(adapter, value, measure, *, limit=30000, correction_reserve=1024):
    catalog=image_catalog(adapter,value['view']['original_evidence'])
    packets=[];group=[]
    def usage(handles):
        view=resource_view(value,set(handles))
        images=select_images(view['original_evidence']['tasks'],catalog)
        prepared=measure('resource-reduce',view,schema('resource-reduce'),images)
        return view, {'input_codepoints':prepared['input_size'],**image_usage(images)}, images_fit(images)
    for candidate in value['view']['candidates']:
        handle=candidate['candidate'];trial=[*group,handle]
        view,size,fit=usage(trial)
        if group and (size['input_codepoints']+correction_reserve>limit or not fit):
            prior_view,prior_size,_=usage(group)
            packets.append({'candidate_refs':group,'view':prior_view,**prior_size})
            group=[];view,size,fit=usage([handle])
        if size['input_codepoints']+correction_reserve>limit or not fit:
            raise ContractError('READING_WINDOW_OVERSIZE','$',json.dumps({
                'contract':CONTRACT,'candidate':handle,'reading_refs':[r['reading'] for r in candidate['source_evidence']],
                **size,'limit':limit,'correction_reserve':correction_reserve,
                'reason':'one candidate with its complete required evidence cannot fit; no truncation or model call'},ensure_ascii=False))
        group.append(handle)
    if group:
        view,size,_=usage(group)
        packets.append({'candidate_refs':group,'view':view,**size})
    if not packets:
        view,size,fit=usage([])
        if not fit or size['input_codepoints']+correction_reserve>limit:
            raise ContractError('READING_WINDOW_OVERSIZE','$','empty-candidate source framing exceeds the bound')
        packets.append({'candidate_refs':[],'view':view,**size})
    return packets


def merge(value, packets, drafts):
    """Validate per-packet coverage first, then preserve the ordinary whole-source contract."""
    proposals=[];omitted=[];seen=set()
    for packet,draft in zip(packets,drafts,strict=True):
        validate_packet(packet,draft)
        refs=[r for p in draft['proposals'] for r in p['candidate_refs']]+draft['omitted_candidate_refs']
        if len(refs)!=len(set(refs)) or set(refs)!=set(packet['candidate_refs']) or seen.intersection(refs):
            raise ContractError('INCOMPLETE_COVERAGE','$','packet must adopt or explicitly omit exactly its own candidates')
        seen.update(refs);proposals.extend(copy.deepcopy(draft['proposals']));omitted.extend(draft['omitted_candidate_refs'])
    if seen!={c['candidate'] for c in value['view']['candidates']}:
        raise ContractError('INCOMPLETE_COVERAGE','$','source packet coverage is incomplete')
    return {'proposals':proposals,'omitted_candidate_refs':omitted,
            'reason':'Program collected source-local packet proposals; global coordination must resolve relationships across packets.'}


def validate_packet(packet,draft):
    validate('resource-reduce',draft)
    if 'blocked_reason' in draft:
        raise ContractError('SEMANTIC_REVIEW_REQUIRED','$',draft['blocked_reason'])
    refs=[r for p in draft['proposals'] for r in p['candidate_refs']]+draft['omitted_candidate_refs']
    if len(refs)!=len(set(refs)) or set(refs)!=set(packet['candidate_refs']):
        raise ContractError('INCOMPLETE_COVERAGE','$','packet coverage must match its exact authorized candidates')
