"""Qualify complete independently readable sections of a held incremental page."""
from collections import defaultdict
from pathlib import PurePosixPath
import re

from hermes_source_units.incremental_pages import require
from hermes_source_units.short_build import identity
from hermes_source_units.validation import ContractError, canonical_json, fingerprint
from incremental_writing_evidence import HEADING
from short_cards import place


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
