"""Bound a second coordination look to provably available held evidence."""
import copy
import re

from hermes_source_units.incremental_pages import normalize_page_changes
from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import ContractError, fingerprint
from short_evidence_pass import request, require

CONTRACT = 'incremental-coordination-repair/v1'
WAITING_FOR_WRITING = re.compile(
    r'^(?:writing_review_pending\b|(?:暂缓[，：: ]*)?(?:待|需先|尚未)(?:阅读|读取|核对|审阅|查阅|检查)'
    r'(?:冻结源|完整|原文|源文|证据|书写|写作)|'
    r'(?:await(?:ing)?|pending|must first|not yet)\s+(?:reading|read|review(?:ing)?|inspect(?:ing)?)\s+'
    r'(?:the\s+)?(?:frozen|source|original|full|complete|evidence))', re.I)
MISSING_EVIDENCE = re.compile(
    r'缺(?:少|失)|不存在|无法(?:获取|读取|核验)|(?:表|图|附件|快照|来源|原文).{0,5}未(?:登记|封存|提供)|'
    r'\b(?:missing|unavailable|unregistered|unreadable|withdrawn|corrupt|not (?:registered|sealed|available))\b',
    re.I)


def eligible_targets(held, inventory, allowed):
    """A deferred writing check is recoverable only with live Map evidence."""
    targets = {}
    for slot, (raw, change) in held.items():
        if (raw['action'] != 'hold' or change['unresolved'] or not raw['support_aliases']
                or not WAITING_FOR_WRITING.search(raw['reason'].strip()) or MISSING_EVIDENCE.search(raw['reason'])):
            continue
        try:
            _, proof = inventory.writing_support(change)
        except (ContractError, ValueError):
            continue
        aliases = set(raw['support_aliases'])
        qualified = set(proof['qualified_aliases'])
        if qualified and qualified <= allowed and any(
                inventory.writing_body(inventory.aliases[alias]) for alias in qualified):
            targets[slot] = {'name': raw['name'], 'required_aliases': sorted(aliases),
                             'allowed_aliases': sorted(aliases | qualified),
                             'required_source_snapshots': sorted(raw['required_source_snapshots']),
                             'hold_sha256': fingerprint(raw), 'evidence_sha256': fingerprint(proof)}
            if change['expected_parent_revision_id']:
                targets[slot]['registered_page_id'] = change['page_id']
    return targets


def ordered_targets(targets):
    return ((slot, targets[slot]) for slot in sorted(targets, key=int))


def corrected_request(original, answer, targets):
    from hermes_source_units.short_build import PAGE_DIRECTORIES

    view = original['view']
    allowed = {key for row in targets.values() for key in row['allowed_aliases']}
    topics = []
    for topic in view['mapping_topics']:
        relations = [r for r in topic['relations'] if r[0] in allowed or allowed.intersection(r[2])]
        if relations:
            topics.append({**topic, 'relations': relations})
    correction = {
        'round': view['round'], 'registered_pages': view['registered_pages'],
        'registered_page_snapshot_sha256': view['registered_page_snapshot_sha256'],
        'mapping_sources': view['mapping_sources'], 'mapping_topics': topics,
        'relation_columns': view['relation_columns'],
        'allowed_output_paths': view.get('allowed_output_paths') or {
            kind: '/'.join([*prefix, *(['{theme}'] if kind == 'card' else []), '{name}.md'])
            for kind, prefix in PAGE_DIRECTORIES.items()},
        'coordination_repair': {'contract': CONTRACT, 'parent_request_id': original['id'],
                                'parent_answer_sha256': fingerprint(answer)},
        'targets': [{'target': slot, **{key: row[key] for key in (
                        'name', 'required_aliases', 'required_source_snapshots', 'registered_page_id') if key in row},
                     **({'additional_aliases': sorted(set(row['allowed_aliases']) - set(row['required_aliases']))}
                        if row['allowed_aliases'] != row['required_aliases'] else {})}
                    for slot, row in ordered_targets(targets)],
        'instructions': (
            'Reconsider only these held decisions. Give at most one decision per target and include its exact target ID. '
            'Planning may propose a page from the registered sealed Map. The later writer reads and verifies the full '
            'original and required tables, qualifiers and assets. Awaiting this normal check does not block planning. '
            'Hold a demonstrably missing clause, asset, dependency or source and name it explicitly. Each new plan '
            'must cover all required_aliases and may also use its additional_aliases. Keep the exact source snapshots '
            'of the target. Use the allowed path template and a stable new identity for create, or the exact supplied '
            'registered page identity and path for update/reuse. A target with registered_page_id must keep that '
            'identity. Do not merge targets or change other pages.'),
    }
    if 'support_material' in view:
        correction['support_material'] = {alias: label for alias, label in view['support_material'].items() if alias in allowed}
    if 'context_support' in view:
        correction['context_support'] = [{**row, 'aliases': sorted(allowed.intersection(row['aliases']))}
                                         for row in view['context_support'] if allowed.intersection(row['aliases'])]
    schema = copy.deepcopy(original['schema']['properties']['results']['properties']['r'])
    item = schema['properties']['changes']['items']
    item['properties']['target'] = {'type': 'string'}
    item['required'].append('target')
    return request(original['phase'], correction, schema, original['budget']['estimated_output_tokens'],
                   original['budget']['input_cap'])


def has_budget(run, requests):
    """Rank bounded follow-ups without consuming the next phase's allowance."""
    reserved = list(run['reservations'].values())
    caps = run['budget']
    phases = sum(row['max_calls'] for row in reserved if row['phase'] == 'coordination')
    remain = {k: caps[k] - sum(row[k] for row in reserved)
              for k in ('max_calls', 'max_input_tokens', 'max_output_tokens')}
    allowed = []
    for row in requests:
        identity = fingerprint({'request': row['id'], 'ordinal': 1})
        if any(x['request_fingerprint'] == identity for x in reserved):
            allowed.append(row)  # Reuse or isolate the existing attempt under its original reservation.
            continue
        cost = {'max_calls': 1, 'max_input_tokens': row['budget'].get('text_tokens') or row['budget']['utf8_bytes'],
                'max_output_tokens': row['output_cap']}
        if phases < caps['phase_limits']['coordination'] and all(remain[k] >= v for k, v in cost.items()):
            allowed.append(row)
            phases += 1
            for k, v in cost.items():
                remain[k] -= v
    return allowed


def corrected_changes(service, frozen, answer, targets, refs, registry, occupied):
    grouped = {}
    for raw in answer['changes']:
        grouped.setdefault(raw['target'], []).append(raw)
    changes = {}
    for slot, target in ordered_targets(targets):
        decisions = grouped.get(slot, [])
        if len(decisions) != 1:
            continue
        raw, = decisions
        aliases = set(raw['support_aliases'])
        if (raw['action'] == 'hold' or not set(target['required_aliases']) <= aliases <= set(target['allowed_aliases'])
                or sorted(raw['required_source_snapshots']) != target['required_source_snapshots']):
            continue
        try:
            result, = normalize_page_changes(service, frozen, {'changes': [raw]}, refs, registered_pages=registry)
        except (ContractError, ValueError):
            continue
        if (result['action'] == 'hold' or result['page_id'] != target.get('registered_page_id', result['page_id'])
                or result['page_id'] in occupied[0] or result['path'].casefold() in occupied[1]):
            continue
        changes[slot] = result
        occupied[0].add(result['page_id'])
        occupied[1].add(result['path'].casefold())
    return changes


def authorize(root, row, ordinal):
    """The native retry cannot acquire a second ordinal or replace the first result."""
    from incremental_native import verified_result

    identity = row['view']['coordination_repair']
    require(ordinal == 1 and row['phase'] == 'incremental-page-plan' and identity['contract'] == CONTRACT,
            'coordination repair may acquire a single ordinal')
    parent = root / 'jobs' / (identity['parent_request_id'] + '.json')
    original = _load_json(parent)['request']
    require(parent.stem == original['id'] and original['phase'] == row['phase']
            and original['view'].get('planning_revision', 0) >= 4
            and 'coordination_repair' not in original['view'], 'coordination repair requires an original coordination request')
    result, _ = verified_result(root, original)
    answer = _load_json(root / 'receipts' / (original['id'] + '.json'))['answer']
    require(result['state'] == 'settled' and len(result['attempts']) == 1
            and result['attempts'][0]['state'] == 'returned'
            and fingerprint(answer) == identity['parent_answer_sha256']
            and original['view']['round'] == row['view']['round'], 'coordination repair lost its verified first attempt')
    approval = _load_json(root / 'coordination-repairs' / (original['id'] + '.json'))
    require(approval['request_id'] == row['id'] and corrected_request(original, answer, approval['targets']) == row,
            'coordination repair differs from its bound local request')
