"""Bound content-outcome recovery to native slot history, not receipt registration."""
import copy
import re
from contextlib import closing

from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import fingerprint
from short_evidence_pass import evidence, require
from short_fragment_repair import CONTENT_CONTRACT, scoped_request
from short_page_contract import CONTRACT, body_request, local_task


def classification(view, receipt, sources):
    answer = receipt['answer']
    block = answer.get('blocked', {})
    notes = block.get('detail', '') + ' ' + ' '.join(x['reason'] for x in answer.get('unresolved', ()))
    if block.get('kind') == 'unreadable' or re.search(r'无法辨认|不可辨认|无法识别|OCR|乱码|unreadable|corrupt', notes, re.I):
        return 'source_quality_deferred'
    # Resolve only explicit registered same-source dependency edges. Section proximity is not authority.
    for ref, item in view['materials'].items():
        source = sources[ref.split('-', 1)[0]]
        for dep in item['required_assets']:
            if (dep not in source['items'] or source['items'][dep]['kind'] != 'asset'
                    or dep.split('-', 1)[0] != ref.split('-', 1)[0]):
                return 'required_evidence_unavailable'
    if block.get('kind') == 'missing_asset' and not block.get('refs'):
        return 'required_evidence_unlocated'
    return 'repair_supply_and_local_scope' if block else 'repair_required_evidence_disposition'


def replacement(row, slot, receipt, limit, sources):
    """Retain original evidence and add only its registered explicit asset closure."""
    original = row['view']['pages'][slot]
    view = copy.deepcopy(original)
    images = list(row['image_inputs'])
    for source_id in sorted({key.split('-', 1)[0] for key in original['materials']}):
        ids = [key for key in original['materials'] if key.startswith(source_id + '-')]
        material, pictures = evidence(sources[source_id], ids)
        for key, item in material.items():
            if key in view['materials']:
                require(item == view['materials'][key], 'content recovery evidence changed')
                continue
            require(item['kind'] == 'asset', 'content recovery cannot expand substantive source scope')
            view['materials'][key] = item
            view['obligations'][key] = {'role': 'asset', 'mapped_roles': ['asset'], 'inspect': True,
                                       'express': True, 'covers_by': [], 'assignment': 'primary'}
        for picture in pictures:
            if picture not in images:
                images.append(picture)
    enriched = {**row, 'view': {**row['view'], 'pages': {**row['view']['pages'], slot: view}}, 'image_inputs': images}
    base = scoped_request(enriched, slot, limit, modern=True)
    view = copy.deepcopy(base['view']['pages']['p1'])
    bindings = {key: {ref: view['materials'][ref]['label'] for ref in item['required_assets']}
                for key, item in view['materials'].items() if item['required_assets']}
    local_task(view, bindings)
    view['content_recovery'] = {
        'contract': CONTENT_CONTRACT, 'prior_receipt_sha256': receipt['receipt_sha256'],
        'classification': classification(original, receipt, sources), 'previous_answer': receipt['answer'],
        'diagnostic': receipt['diagnostic'], 'maximum_additional_calls': 1,
    }
    view['repair_of_fragment'] = original['fragment_id']
    view.pop('fragment_id', None)
    view['fragment_id'] = fingerprint([CONTRACT, CONTENT_CONTRACT, view])
    return body_request([{'view': view, 'images': images}], limit, repair_contract=CONTENT_CONTRACT)


def slot_attempts(root, fragment_id):
    """Reservations count even when completion is unknown; sibling retries do not count."""
    records = []
    for path in sorted((root / 'jobs').glob('*.attempt-*.json')):
        reserved = _load_json(path)
        call = reserved['request']
        for slot, view in call['view'].get('pages', {}).items():
            if fragment_id not in (view.get('fragment_id'), view.get('repair_of_fragment')):
                continue
            if slot not in call.get('missing_slots', call['view']['pages']):
                continue
            records.append(path.relative_to(root).as_posix())
    return records


def plan_recovery(root, plan, sources, runner, *, section_delivery=True):
    """Authenticate held results before offering their one remaining content call."""
    attempts = runner.attempt_index(root)
    _, kc, _ = runner.runtime()
    changes, decisions = [], []
    with closing(kc.connect(board=_load_json(root / 'operation.json')['board'])) as conn:
        for receipt in plan['checks']:
            if receipt['state'] != 'held':
                continue
            fid = receipt['fragment_id']
            view, proof = runner_native(root, receipt, attempts, conn, runner)
            spent = slot_attempts(root, fid)
            cause = classification(view, receipt, sources)
            decision = {'fragment_id': fid, 'receipt_sha256': receipt['receipt_sha256'],
                        'classification': cause, 'reservations': spent, 'native_proof': proof}
            if len(spent) != 1:
                decision['disposition'] = 'isolate_attempt_limit_or_unknown'
            elif cause in ('source_quality_deferred', 'required_evidence_unavailable', 'required_evidence_unlocated'):
                decision['disposition'] = 'isolate_evidence_limitation'
            else:
                decision['disposition'] = 'one_targeted_content_call'
                changes.append({'fragment_id': fid, 'action': 'content',
                                'prior_receipt_sha256': receipt['receipt_sha256'], 'donor_fragment_id': None})
            decisions.append(decision)
    for hold in plan['held']:
        if hold.get('fragment_id') and not any(d['fragment_id'] == hold['fragment_id'] for d in decisions):
            decisions.append({'fragment_id': hold['fragment_id'], 'classification': hold['reason'],
                              'disposition': 'isolate_no_eligible_held_result'})
    return {'contract': CONTENT_CONTRACT, 'expected_pages_sha256': fingerprint(plan['expected_pages']),
            'changes': changes, 'decisions': decisions, 'section_delivery': section_delivery}


def runner_native(root, receipt, attempts, conn, runner):
    from short_formal_build import native_fragment
    return native_fragment(root, receipt, attempts, conn, runner)
