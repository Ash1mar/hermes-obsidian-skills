"""Use the existing Pass renderer and wire budget for future exact plans."""
import copy
import json
from types import SimpleNamespace

from hermes_source_units.validation import fingerprint
from hermes_source_units.semantic_budget import PHASE_BALANCED_POLICY, budget_policy_pin
from fixed_semantic import COMMON, RULES, WIRE_CONTRACT, group_usage, OUTPUT_TOKENS, OUTPUT_RESERVE
from fixed_semantic_schema import slot_schema, strict_schema
from fixed_semantic_media import image_catalog, image_budget
from native_pass_identity import visible_packet

PLANNING_POLICY={'version':'sequential-model-budget/v2', 'model_input_codepoints':30000,
    'future_candidate_codepoints_per_estimated_token':3,
    'future_candidate_bytes_per_estimated_token':6}


def model_measurement(service, actor, registry, limit=None):
    limit = PLANNING_POLICY['model_input_codepoints'] if limit is None else limit
    identity={'planning':PLANNING_POLICY, 'phase_budget':budget_policy_pin(), 'input_limit':limit,
        'image_budget':image_budget(), 'request_contract_sha256':fingerprint({
            'wire_contract':WIRE_CONTRACT,
            'common':COMMON, 'rules':{p:RULES[p] for p in ('candidate','citation')},
            'schema':{p:strict_schema(slot_schema(p,[{'task':'t1'}])) for p in ('candidate','citation')}})}
    def measure(refs, assembled):
        projection=service.preview_reading_model_input(refs, assembled, actor, registry)
        packet=visible_packet(service.model_packet([projection], 'read-only-planning')['view'])
        adapter=SimpleNamespace(workflow=SimpleNamespace(vault=service.vault, knowledge=service))
        images=image_catalog(adapter,packet)
        candidate=group_usage(packet,packet['tasks'],'candidate',limit,images,PHASE_BALANCED_POLICY)
        citation_packet=copy.deepcopy(packet)
        # Only sizing placeholders; never written as Passes or sent to a model.
        citation_packet['tasks'][0]['continuation']={'candidate':{'candidates':[], 'empty_reason':''}}
        citation=group_usage(citation_packet,citation_packet['tasks'],'citation',limit,images,PHASE_BALANCED_POLICY)
        tokens=candidate['estimated_output_tokens']
        citation['future_candidate_input_reserve_codepoints']=tokens*PLANNING_POLICY['future_candidate_codepoints_per_estimated_token']
        citation['input_codepoints']+=citation['future_candidate_input_reserve_codepoints']
        citation['estimated_output_tokens']=max(citation['estimated_output_tokens'],
            (tokens*PLANNING_POLICY['future_candidate_bytes_per_estimated_token']+3)//4)
        if citation['input_codepoints']+citation['correction_reserve_codepoints']>limit:
            citation['blocking_reasons'].append('future_citation_input')
        if citation['estimated_output_tokens']>OUTPUT_TOKENS-OUTPUT_RESERVE:
            citation['blocking_reasons'].append('future_citation_output')
        reasons=[reason for usage in (candidate,citation) for reason in usage['blocking_reasons']]
        code='SEMANTIC_IMAGE_OVERSIZE' if 'whole_image_budget' in reasons else 'SEMANTIC_OUTPUT_OVERSIZE' if any('output' in r for r in reasons) else 'READING_WINDOW_OVERSIZE' if reasons else None
        return {'fits':not reasons,'blocking_code':code,'candidate':candidate,'citation':citation,
            'projection_sha256':fingerprint(packet),'policy_sha256':fingerprint(identity),
            'future_candidate_reserve':'conservative byte/character heuristic; future native output remains runtime-checked'}
    measure.identity=identity
    return measure
