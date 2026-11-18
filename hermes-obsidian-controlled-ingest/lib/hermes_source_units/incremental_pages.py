"""Page identities and evidence-bound revisions for explicit incremental rounds."""
from __future__ import annotations

import copy
import re

from .source_units import _load_json, _sha, _vault_path
from .validation import ContractError, canonical_json, fingerprint


CHANGE = 'incremental-page-change/v1'
AUTHORIZATION = 'incremental-build-authorization/v1'
BUILD_EVIDENCE = 'native-incremental-build/v1'


def require(condition, message):
    if not condition:
        raise ContractError('INCREMENTAL_PAGE_INVALID', '$', message)


def unique_refs(refs):
    return list({canonical_json(ref): copy.deepcopy(ref) for ref in refs}.values())


def revision_record(service, subject):
    path = _vault_path(service.vault, '_system/knowledge-builds/page-revisions/'
                       f"{subject['page_id']}/{subject['current_revision_id']}.json")
    page = _load_json(path)
    require(page['page_id'] == subject['page_id'] and page['subject_id'] == subject['subject_id']
            and page['revision_id'] == subject['current_revision_id'] and page['state'] == 'committed'
            and page['path'] == subject['current_path'], 'registered page revision differs from its identity')
    body = _vault_path(service.vault, page['path']).read_bytes()
    require(_sha(body) == page['authored_sha256'], 'registered page body differs from its revision')
    return page, body


def registry_view(service):
    """Expose registered identity and parent pins without matching pages by title."""
    rows = []
    for subject in service._registry()['subjects']:
        if subject['status'] != 'active':
            continue
        try:
            page, _ = revision_record(service, subject)
        except (ContractError, OSError) as error:
            rows.append({'page_id': subject['page_id'], 'subject_id': subject['subject_id'],
                         'identity': {key: subject[key] for key in ('kind', 'identity_key', 'canonical_name', 'aliases')},
                         'path': subject['current_path'], 'page_type': subject.get('page_type', 'card'),
                         'revision_id': subject['current_revision_id'], 'held': True, 'reason': str(error)})
            continue
        rows.append({'page_id': subject['page_id'], 'subject_id': subject['subject_id'],
                     'identity': {key: subject[key] for key in ('kind', 'identity_key', 'canonical_name', 'aliases')},
                     'path': page['path'], 'page_type': page.get('page_type', 'card'),
                     'legacy_untyped': 'page_type' not in page,
                     'revision_id': page['revision_id'], 'authored_sha256': page['authored_sha256'],
                     'support_refs': page['support_refs']})
    return rows


def plan_page_changes(service, round_input, maps):
    """Build bounded native coordination input; the native worker makes decisions."""
    require(round_input['contract'] == 'incremental-round-input/v1' and maps
            and {row['map_result_id'] for row in maps} == set(round_input['map_result_ids']) | set(round_input.get('context_map_result_ids', [])),
            'coordination requires the exact frozen mapping result set')
    properties = {
        'action': {'type': 'string', 'enum': ['create', 'update', 'reuse', 'hold']},
        'page_id': {'type': 'string'}, 'identity_key': {'type': 'string'},
        'name': {'type': 'string'}, 'path': {'type': 'string'},
        'page_type': {'type': 'string', 'enum': ['card', 'concept', 'entity', 'topic', 'project']},
        'support_aliases': {'type': 'array', 'items': {'type': 'string'}},
        'required_source_snapshots': {'type': 'array', 'items': {'type': 'string'}},
        'reason': {'type': 'string'},
    }
    schema = {'type': 'object', 'properties': {'changes': {'type': 'array', 'items': {
        'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}},
        'required': ['changes'], 'additionalProperties': False}
    view = {'round': copy.deepcopy(round_input), 'mapping_results': copy.deepcopy(maps),
            'registered_pages': registry_view(service),
            'instructions': 'Choose explicit page identities. A title is not identity. Preserve distinct scopes. '
                            'Update only a supplied registered page_id. New pages require a stable scope identity_key. '
                            'Retain old evidence, conditions and exceptions. Defer pages requiring unready sources. '
                            'Use only supplied support aliases. Every mapping item needs a page or hold disposition.'}
    return {'phase': 'incremental-page-plan', 'view': view, 'schema': schema}


def normalize_page_changes(service, round_input, answer, refs_by_alias, scope_metadata=None, *, registered_pages=None):
    """Bind native decisions to exact parents and authorized source references."""
    from .knowledge_destinations import governed
    from .short_build import output_path

    # A replay uses only the registered-page view from its verified original
    # native prompt. Admission and Build still validate live parents separately.
    known = {row['page_id']: row for row in (registry_view(service) if registered_pages is None else registered_pages)}
    snapshot_ids = set(round_input['snapshots'])
    metadata = scope_metadata or {}
    require(set(metadata) <= {'native_authorization_ref', 'budget_ref'}, 'page metadata cannot override change identity')
    changes = []
    seen_pages, seen_paths = set(), set()
    for raw in answer['changes']:
        action = raw['action']
        require(action in ('create', 'update', 'reuse', 'hold'), 'unsupported page change action')
        require(raw['reason'].strip() and raw['name'].strip(), 'page disposition requires a reason and name')
        requested = raw['required_source_snapshots']
        require(len(requested) == len(set(requested)), 'duplicate required source snapshot')
        missing = sorted(set(requested) - snapshot_ids)
        if missing:
            action = 'hold'
        require(all(alias in refs_by_alias for alias in raw['support_aliases']), 'page support alias is outside the request')
        refs = unique_refs(refs_by_alias[key] for key in raw['support_aliases'])
        page_id = raw['page_id']
        prior = known.get(page_id)
        if action in ('update', 'reuse'):
            require(prior is not None and not prior.get('held') and raw['path'] == prior['path']
                    and raw['page_type'] == prior['page_type'], 'update requires an exact registered identity and path')
        elif action == 'create':
            require(not page_id and raw['identity_key'].strip(), 'create requires a new explicit stable scope identity')
        if prior:
            identity = prior['identity']
            parent = prior['revision_id']
            parent_hash = prior.get('authored_sha256')
            retained = prior.get('support_refs', [])
            subject_id = prior['subject_id']
        else:
            identity = {'kind': 'concept', 'identity_key': 'incremental:' + fingerprint({
                'scope': raw['identity_key'], 'page_type': raw['page_type']}),
                'canonical_name': raw['name'], 'aliases': []}
            subject_id = fingerprint({'vault_id': service.source.vault_id, 'kind': identity['kind'],
                                      'identity_key': identity['identity_key']})
            page_id = fingerprint({'vault_id': service.source.vault_id, 'subject_id': subject_id})
            parent = parent_hash = None
            retained = []
            if action == 'create':
                require(page_id not in known, 'existing stable scope must be updated explicitly')
                output_path('unused', raw['path'], page_type=raw['page_type'])
                require(governed(raw['path']) and (registered_pages is not None
                        or not _vault_path(service.vault, raw['path']).exists()),
                        'new page destination is already occupied')
        if action != 'hold':
            require(refs or retained, 'a page requires actual source support')
            require(page_id not in seen_pages and raw['path'].casefold() not in seen_paths,
                    'one round can change a page or path only once')
            seen_pages.add(page_id)
            seen_paths.add(raw['path'].casefold())
        added = [ref for ref in refs if canonical_json(ref) not in {canonical_json(old) for old in retained}]
        if action == 'reuse':
            require(not added, 'new evidence requires an explicit update disposition')
        change = {'contract': CHANGE, 'run_id': round_input['run_id'], 'round_id': round_input['round_id'],
                  'action': action, 'page_id': page_id, 'subject_id': subject_id, 'identity': identity,
                  'name': raw['name'], 'page_type': raw['page_type'], 'path': raw['path'],
                  'legacy_untyped': bool(prior and prior.get('legacy_untyped')),
                  'expected_parent_revision_id': parent, 'parent_authored_sha256': parent_hash,
                  'input_source_snapshots': sorted(snapshot_ids), 'mapping_receipts': round_input['map_result_ids'],
                  'retained_support_refs': retained, 'added_support_refs': added, 'removed_support_refs': [],
                  'coverage_scope': {'basis': 'declared_sources', 'snapshots': requested},
                  'required_assets': [], 'unresolved': [{'missing_snapshot': key} for key in missing],
                  'reason': raw['reason'], 'round_fingerprint': round_input['input_fingerprint'], **metadata}
        change['change_id'] = fingerprint(change)
        changes.append(change)
    require(changes, 'coordination must record at least one page or held disposition')
    return changes


def validate_parent(service, change, *, allow_committed=False, authored_sha256=None):
    require(change['contract'] == CHANGE and change['change_id'] == fingerprint({
        key: value for key, value in change.items() if key != 'change_id'}), 'page change identity changed')
    registry = {row['page_id']: row for row in service._registry()['subjects'] if row['status'] == 'active'}
    subject = registry.get(change['page_id'])
    if change['action'] == 'create':
        require(subject is None or allow_committed, 'new page identity is already registered')
        require(allow_committed or not _vault_path(service.vault, change['path']).exists(),
                'new page destination is already occupied')
        return
    require(subject and subject['subject_id'] == change['subject_id'] and subject['current_path'] == change['path']
            and subject['identity_key'] == change['identity']['identity_key'], 'registered page identity changed')
    page, _ = revision_record(service, subject)
    require(bool(change.get('legacy_untyped')) == ('page_type' not in page)
            and change['page_type'] == page.get('page_type', 'card'), 'registered page type changed after coordination')
    if allow_committed and authored_sha256 == page['authored_sha256']:
        return
    require(page['revision_id'] == change['expected_parent_revision_id']
            and page['authored_sha256'] == change['parent_authored_sha256'], 'page parent changed after coordination')
    require(unique_refs(page['support_refs']) == unique_refs(change['retained_support_refs'])
            and change['removed_support_refs'] == [], 'incremental update must retain every existing source support')


def prepare_page_revision(service, change, actor):
    """Read the complete prior body and current original evidence for native writing."""
    require(change['action'] in ('create', 'update') and not change['unresolved'], 'held or reused page needs no writing')
    validate_parent(service, change)
    refs = unique_refs([*change['retained_support_refs'], *change['added_support_refs']])
    registry = service._document_registry_revision()
    reader = service._reader_config()
    assembled = service._assemble_reading(refs, actor, registry, reader)
    require(assembled['measurement']['fits'], 'page evidence exceeds the complete reading budget')
    projection = service.preview_reading_model_input(refs, assembled, actor, registry)
    parent = None
    if change['expected_parent_revision_id'] is not None:
        parent = _vault_path(service.vault, change['path']).read_text(encoding='utf-8')
    properties = {'content': {'type': 'string'}, 'inspected': {'type': 'array', 'items': {'type': 'string'}},
                  'unresolved': {'type': 'array', 'items': {'type': 'string'}}}
    prepared = {'phase': 'incremental-page-write', 'view': {'change': change, 'parent_content': parent,
            'original_evidence': projection['view'],
            'instructions': 'Write the complete new Markdown revision. Preserve necessary existing conditions and '
                            'exceptions using original evidence. Inspect all core and required assets. Cite supplied '
                            'material handles. Return unresolved evidence explicitly; never infer unread content.'},
            'schema': {'type': 'object', 'properties': properties, 'required': list(properties),
                       'additionalProperties': False},
            'material_aliases': projection['mapping'], 'projection': projection,
            'support_refs': refs, 'reader_measurement': assembled['measurement']}
    prepared['view']['preparation_fingerprint'] = preparation_fingerprint(prepared)
    return prepared


def preparation_fingerprint(prepared):
    value = copy.deepcopy(prepared)
    value['view'].pop('preparation_fingerprint', None)
    return fingerprint(value)


def validate_preparation(service, prepared, actor):
    """Check saved aliases and live source content without rebasing a native input."""
    require(prepared['view'].get('preparation_fingerprint') == preparation_fingerprint(prepared),
            'saved page preparation fingerprint changed')
    fresh = prepare_page_revision(service, prepared['view']['change'], actor)
    # Registry revision participates in input_id, while unrelated new source
    # registration must not invalidate unchanged original material and aliases.
    def semantic_view(value):
        view = copy.deepcopy(value['view'])
        view.pop('preparation_fingerprint', None)
        view['original_evidence'].pop('input_id', None)
        return view
    old_measurement = {k: v for k, v in prepared['reader_measurement'].items() if k != 'input_fingerprint'}
    new_measurement = {k: v for k, v in fresh['reader_measurement'].items() if k != 'input_fingerprint'}
    require(semantic_view(fresh) == semantic_view(prepared)
            and fresh['phase'] == prepared['phase'] and fresh['schema'] == prepared['schema']
            and fresh['material_aliases'] == prepared['material_aliases'] == prepared['projection']['mapping']
            and fresh['support_refs'] == prepared['support_refs'] and old_measurement == new_measurement,
            'live page evidence, reader policy or alias mapping changed after writing')


def projection(service, row):
    """Project an explicitly authorized create/update onto the generic Build contract."""
    change = row['incremental_change']
    require(change['contract'] == CHANGE and change['change_id'] == fingerprint({
                key: value for key, value in change.items() if key != 'change_id'})
            and change['action'] in ('create', 'update') and row['short_id'] == change['change_id']
            and row['support_refs'] == unique_refs([*change['retained_support_refs'], *change['added_support_refs']]),
            'Build page differs from its incremental change')
    require((change['action'] == 'create') == (change['expected_parent_revision_id'] is None)
            and (change['action'] == 'create') == (change['parent_authored_sha256'] is None),
            'page action differs from its pinned parent')
    require(not change.get('legacy_untyped') or change['action'] == 'update',
            'legacy absence of page type can only be preserved on an existing revision')
    require(change['page_id'] == fingerprint({'vault_id': service.source.vault_id, 'subject_id': change['subject_id']}),
            'page ID differs from its stable subject')
    subject = {**change['identity'], 'subject_id': change['subject_id']}
    require(subject['subject_id'] == fingerprint({'vault_id': service.source.vault_id, 'kind': subject['kind'],
                                                 'identity_key': subject['identity_key']}), 'subject identity differs')
    page = {'contract': 'hermes-knowledge-page-revision/v1', 'page_id': change['page_id'],
            'subject_id': change['subject_id'], 'parent_revision_id': change['expected_parent_revision_id'],
            'action': change['action'], 'path': change['path'], 'authored_sha256': row['authored_sha256'],
            'support_refs': row['support_refs'], 'qa_status': row['qa_status'],
            'business_status': 'unassessed', 'visibility': 'draft'}
    if not change.get('legacy_untyped'):
        page['page_type'] = change['page_type']
    page['revision_id'] = fingerprint({key: value for key, value in page.items() if key != 'contract'})
    decision = {key: page[key] for key in ('subject_id', 'page_id', 'action', 'path', 'support_refs', 'revision_id')}
    return {**page, 'state': 'draft', 'review': None}, {**decision, 'candidate_refs': [], 'identity': subject}


def validate_page_revision(prepared, response):
    """Check complete inspection and support boundaries before staging native writing."""
    require(prepared['view'].get('preparation_fingerprint') == preparation_fingerprint(prepared)
            and prepared['material_aliases'] == prepared['projection']['mapping'],
            'saved page preparation fingerprint or alias mapping changed')
    require(isinstance(response, dict) and set(response) == {'content', 'inspected', 'unresolved'}
            and isinstance(response['content'], str) and response['content'].strip()
            and response['unresolved'] == [], 'page writing retained unresolved material')
    view = prepared['projection']['view']
    aliases = prepared['material_aliases']
    required = {row['ref'] for row in view['materials']
                if row['role'] == 'core' or row.get('required_dependency')}
    inspected = response['inspected']
    require(isinstance(inspected, list) and len(inspected) == len(set(inspected))
            and set(inspected) <= aliases.keys() and required <= set(inspected),
            'page writing lacks authorized original or required-asset inspection')
    citations = set(re.findall(r'\[(m[0-9]+)\]', response['content']))
    require(citations and citations <= aliases.keys(), 'page writing contains missing or foreign evidence citations')
    cited = {canonical_json(aliases[alias]) for alias in citations}
    require({canonical_json(ref) for ref in prepared['support_refs']} <= cited,
            'page writing omitted a retained or added evidence responsibility')
    body = (response['content'].replace('\r\n', '\n').replace('\r', '\n').rstrip() + '\n').encode('utf-8')
    return {'body': body, 'authored_sha256': _sha(body), 'support_refs': prepared['support_refs'],
            'citation_bindings': {alias: aliases[alias] for alias in sorted(citations)},
            'inspected': sorted(inspected), 'projection_sha256': fingerprint(prepared['projection'])}


def build_scope(operation, authorization, pages, excluded):
    require(authorization['contract'] == AUTHORIZATION and authorization['run_id'] == operation['incremental']['run_id']
            and authorization['round_id'] == operation['incremental']['scope_id']
            and authorization['pages'] == [page['incremental_change']['change_id'] for page in pages],
            'incremental Build authorization differs from its selected round changes')
    return {'actor': authorization['actor'], 'authorization': authorization, 'origin': operation['origin'],
            'coverage_complete': False, 'operation_sha256': operation['operation_sha256'],
            'pages': pages, 'excluded': excluded}
