"""Qualify complete independently readable sections of a held incremental page."""
from collections import defaultdict
from pathlib import PurePosixPath
import re

from hermes_source_units.incremental_pages import require
from hermes_source_units.incremental_sources import pinned, read_pin
from hermes_source_units.short_build import identity
from hermes_source_units.validation import ContractError, canonical_json, fingerprint
from incremental_writing_evidence import HEADING
from short_cards import place

ORDINARY_SECTION = 'incremental-independent-section-selection/v1'
ORDINARY_QUALIFICATION = 'incremental-independent-section-qualification/v1'


def aliases(catalogue, refs):
    return {catalogue.units[canonical_json(ref['unit_ref'])]['alias'] for ref in refs}


def proposals(catalogue, plan, work, slots):
    """Derive a section only from complete, uniquely bounded registered coverage."""
    if (plan['action'] != 'create' or plan['page_type'] == 'project'
            or re.search(r'对比|比较|compar(?:e|ison)', plan['name'], re.I)):
        return [], [{'reason': 'parent scope is not eligible for independent sections'}]
    owners = {}
    by_section = defaultdict(set)
    for key, row in slots.items():
        owners[key] = aliases(catalogue, row['fragment']['prepared']['support_refs'])
        for alias in owners[key]:
            item = catalogue.aliases[alias]
            by_section[item['snapshot'], item['section']].add(key)
    declared = aliases(catalogue, plan['added_support_refs'])
    proposed, deferred, covered = [], [], set()
    for (snapshot, section_id), members in sorted(by_section.items()):
        section = catalogue.sections[snapshot][section_id]
        title = section['title'].strip()
        if not HEADING.fullmatch(title):
            deferred.append({'snapshot': snapshot, 'section': section_id, 'reason': 'no precise numbered section boundary'})
            continue
        # A fragment spanning sections cannot be reassigned by cutting its prose.
        relevant = set().union(*(owners[key] for key in members))
        if (not relevant & declared or relevant & covered
                or any((catalogue.aliases[key]['snapshot'], catalogue.aliases[key]['section'])
                        != (snapshot, section_id) for key in relevant)):
            continue
        whole = {key for key, item in catalogue.aliases.items()
                 if item['snapshot'] == snapshot and any(s['section_id'] == section_id for s in catalogue.lineage(item))
                 and item['locator']['kind'] == 'text' and catalogue.writing_body(item)}
        if not whole or not whole <= relevant or any(slots[key]['state'] != 'valid' for key in members):
            deferred.append({'snapshot': snapshot, 'section': section_id,
                             'reason': 'registered section content or a required fragment is incomplete'})
            continue
        edges = {tuple(pair) for pair in (*work['selection']['dependencies'],
                                        *work['selection']['clause_cross_references'])}
        try:
            edges.update(catalogue.clause_continuations(whole))
            edges.update(catalogue.explicit_dependencies(whole))
        except ContractError as error:
            deferred.append({'snapshot': snapshot, 'section': section_id, 'reason': str(error)})
            continue
        if any(left in relevant and right not in relevant for left, right in edges):
            deferred.append({'snapshot': snapshot, 'section': section_id,
                             'reason': 'required evidence crosses the independently written section'})
            continue
        # Each fragment must itself be independently readable. Its complete
        # projections and coherent citations are checked before it reaches here.
        if any(not aliases(catalogue, slots[key]['fragment']['prepared']['material_aliases'].values()) <= relevant
               for key in members):
            deferred.append({'snapshot': snapshot, 'section': section_id, 'reason': 'fragment reads material outside the section'})
            continue
        covered.update(relevant)
        parts = [key for key in slots if key in members]
        sid = fingerprint(['incremental-independent-section/v1', plan['page_id'],
                           catalogue.aliases[min(whole)]['ref']['unit_ref']['resource_id'], section_id])
        proposed.append({'id': sid, 'name': title, 'page_type': 'card',
                         'theme_directory': PurePosixPath(plan['path']).parent.name,
                         'parent_plan_id': plan['change_id'], 'parent_page_id': plan['page_id'], 'snapshot': snapshot,
                         'section': section_id, 'fragment_ids': parts,
                         'dependency_edges': sorted([list(edge) for edge in edges if edge[0] in relevant])})
    return proposed, deferred


def candidates(service, layout, plans, proposed, page_slots):
    selected = []
    for item in place(layout, proposed):
        if 'registered_path' in item:
            continue
        plan = plans[item['parent_plan_id']]
        parts = [page_slots[plan['change_id']][key] for key in item['fragment_ids']]
        support = [ref for row in parts for ref in row['fragment']['prepared']['support_refs']]
        require(len(set(map(canonical_json, support))) == len(support), 'independent section repeats an evidence duty')
        subject, page_id = identity(service, item['id'], item['name'])
        provenance = {key: item[key] for key in ('parent_plan_id', 'parent_page_id', 'snapshot', 'section',
                                               'fragment_ids', 'dependency_edges')}
        provenance['receipts'] = [row['proof'] for row in parts]
        change = {**plan, 'action': 'create', 'page_id': page_id, 'subject_id': subject['subject_id'],
                  'identity': subject, 'name': item['name'], 'path': item['card_path'], 'page_type': 'card',
                  'legacy_untyped': False, 'expected_parent_revision_id': None, 'parent_authored_sha256': None,
                  'added_support_refs': support, 'retained_support_refs': [], 'removed_support_refs': [],
                  'reason': 'complete independently supported section of a held page',
                  'independent_section': provenance}
        change.pop('change_id')
        change['change_id'] = fingerprint(change)
        selected.append((change, parts))
    return selected


def ordinary_candidates(vault, service, catalogue, writer, layout, parents, *, placements=None):
    """Select independent sections only from the original, strictly valid native receipts."""
    from incremental_writing import compose, fragment_receipt
    from incremental_writing_amendment import validate_fragment

    plans, page_slots, deferred, proposed = {}, {}, {}, []
    with service.source.reading_snapshot():
        for plan, work in parents:
            plan_id = plan['change_id']
            plans[plan_id] = plan
            slots = {}
            for fragment in work['fragments']:
                slot = {'fragment': fragment, 'state': 'held'}
                try:
                    answer, proof = fragment_receipt(vault, writer, fragment)
                    validate_fragment(fragment, answer)
                except (ContractError, ValueError, OSError):
                    pass
                else:
                    slot.update(state='valid', answer=answer, proof=proof)
                slots[fragment['request']['id']] = slot
            page_slots[plan_id] = slots
            try:
                selected, deferred[plan_id] = proposals(catalogue, plan, work, slots)
            except (ContractError, ValueError, OSError) as error:
                deferred[plan_id] = [{'reason': str(error)}]
                continue
            proposed.extend(selected)

    cards, used = [], set()
    available = {**layout, 'occupied': list(layout['occupied']), 'directories': list(layout['directories'])}
    if placements is None:
        ordered = [(item, None) for item in sorted(proposed, key=lambda row: row['id'])]
    else:
        by_identity = {(item['id'], item['snapshot']): item for item in proposed}
        require(len({item['id'] for item in placements}) == len(placements), 'pinned independent section identity repeats')
        ordered = [(by_identity.get((fixed['id'], fixed['snapshot'])), fixed) for fixed in placements]
    for item, fixed in ordered:
        occupied = fixed['path'] if fixed else None
        if item is not None:
            try:
                require(item['id'] not in used, 'independent section already qualified in this round')
                selected = candidates(service, available, plans, [item], page_slots)
                require(selected, 'independent section identity already has a registered path')
                (change, entries), = selected
                require(fixed is None or (change['path'], change['change_id']) == (occupied, fixed['change_id']),
                        'independent section differs from its pinned selection')
                compose(service, change, ((entry['fragment'], entry['answer']) for entry in entries))
            except (ContractError, ValueError, OSError) as error:
                deferred[item['parent_plan_id']].append(
                    {'section': item['section'], 'snapshot': item['snapshot'], 'reason': str(error)})
            else:
                cards.append((change, entries))
                used.add(item['id'])
                occupied = change['path']
        if occupied:
            # Keep an earlier pinned path reserved for later candidates even
            # when its own evidence is no longer eligible on reentry.
            available['occupied'].append(occupied)
            available['directories'].append(PurePosixPath(occupied).parent.name)
    return cards, deferred


def section_placements(cards):
    return [{'id': change['identity']['identity_key'].removeprefix('short-evidence:'),
             'snapshot': change['independent_section']['snapshot'], 'path': change['path'],
             'change_id': change['change_id']} for change, _ in cards]


def ordinary_record(change, entries, pointer):
    return {'contract': ORDINARY_SECTION, 'change': change, 'qualification': pointer,
            'proofs': [entry['proof'] for entry in entries]}


def ordinary_formal_pages(domain, builder, frozen, catalogue, pointer, writer):
    """Requalify original frozen plans and receipts at Build ingress."""
    from incremental_writing import CONTRACT, prepare_work

    run_id, round_id = frozen['run_id'], frozen['round_id']
    round_root = domain.root(run_id) / 'rounds' / round_id
    vault = domain.vault
    manifest = read_pin(vault, pointer)
    require(pointer['ref'] == (writer / 'section-qualification.json').relative_to(vault).as_posix()
            and writer == domain.root(run_id) / 'native' / 'round' / round_id / 'writing'
            and manifest['contract'] == ORDINARY_QUALIFICATION and manifest['run_id'] == run_id
            and manifest['round_id'] == round_id and manifest['round_input_fingerprint'] == frozen['input_fingerprint']
            and manifest['plans'] == pinned(vault, (round_root / 'page-changes.json').relative_to(vault).as_posix()),
            'independent section lost its original ordinary writing scope')
    plans = {plan['change_id']: plan for plan in read_pin(vault, manifest['plans'])}
    layout = manifest['layout']
    require({row['path'] for row in plans.values() if row.get('path')} <= set(layout['occupied']),
            'independent section layout lost a frozen page path')
    parents, seen = [], set()
    for entry in manifest['parents']:
        plan_id = entry['plan_id']
        require(plan_id in plans and plan_id not in seen, 'independent section parent scope changed')
        plan = plans[plan_id]
        work_pointer = entry['work']
        require(work_pointer['ref'] == (writer / 'fragment-plans' / (plan_id + '.json')).relative_to(vault).as_posix(),
                'independent section work belongs to a different page')
        work = read_pin(vault, work_pointer)
        require(work['contract'] == CONTRACT and work['plan_id'] == plan_id
                and prepare_work(builder, catalogue, plan, input_cap=work['input_cap'],
                                 writing_revision=work.get('writing_revision', 1)) == work,
                'independent section original fragment plan changed')
        parents.append((plan, work))
        seen.add(plan_id)
    cards, _ = ordinary_candidates(vault, builder, catalogue, writer, layout, parents, placements=manifest['placements'])
    return {change['change_id']: (ordinary_record(change, entries, pointer), entries) for change, entries in cards}
