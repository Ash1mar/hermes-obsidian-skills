"""Isolated Pass contract for mapping complete source units to topics."""
import base64
import copy
import hashlib

from fixed_semantic_media import IMAGE_TYPES, images_fit
from hermes_source_units.reading_dependencies import required_reading
from hermes_source_units.semantic_submission import closed
from hermes_source_units.source_units import (
    ARTIFACT_ROOT, FileSourceUnitService, _child_path, _load_json, _normalize_text,
)
from hermes_source_units.validation import _check_shape, fingerprint

CONTRACT = 'short-evidence-ingest/v1'
RECEIPT = 'short-evidence-result/v1'
PASS_LIMIT = 30_000
OUTPUT_LIMIT = 8192
USABLE_OUTPUT = 7168
SEED_RESERVE = 6000
NAME = {'type': 'string', 'minLength': 1, 'maxLength': 80}
NOTE = {'type': 'string', 'maxLength': 160}
REF = {'type': 'string', 'pattern': r'^[A-Za-z0-9_-]{1,40}$'}
TOPIC = closed({'id': REF, 'name': NAME, 'scope': {**NOTE, 'minLength': 1}})
ROLES = ['evidence', 'condition', 'exception', 'definition', 'context', 'contrary', 'conflict', 'asset']


def array(item):
    return {'type': 'array', 'items': item, 'uniqueItems': True}


LINK = closed({'topic': REF, 'role': {'enum': ROLES}, 'applies_to': array(REF), 'note': NOTE})
DISPOSITION = closed({
    'item': REF, 'status': {'enum': ['linked', 'excluded', 'pending']},
    'links': array(LINK), 'reason': NOTE,
})
SEED_SCHEMA = closed({'topics': array(TOPIC)})
MAP_SCHEMA = closed({'topics': array(TOPIC), 'dispositions': array(DISPOSITION)})
RULES = {
    'short-topics': (
        'Propose initial engineering topics in Chinese from this source outline. '
        'Use short stable local topic IDs, names and scopes. Headings are navigation, '
        'and later evidence mapping may add topics. Do not write findings or page text.'
    ),
    'short-map': (
        'Read every assigned original unit and complete attached asset. Return exactly one disposition '
        'for each assigned ID. Link each relevant item to seed topics or declare new local topics. '
        'Use role and applies_to (visible same-source item IDs) for evidence, conditions, exceptions, '
        'definitions, contrary evidence, conflicts and context. Use asset for whole assets. '
        'Use note only for relations that need explanation. Context-only IDs are not assigned. '
        'Explain exclusions and unresolved items; use pending for ambiguous, missing or unreadable '
        'required evidence. Preserve QA limits and do not rewrite facts as candidate statements.'
    ),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_sources(vault, pins):
    """Read exact approved UnitSets, normalized text and registered whole assets."""
    source = FileSourceUnitService(vault)
    registry = source.vault_manifest['governance']['repository']['registry_path']
    result = {}
    with source.reading_snapshot(verify_hashes=True):
        revision = _load_json(source.track_reading_file(registry))['registry_revision']
        access = {'actor': 'short-evidence-offline', 'purpose': 'construction', 'registry_revision': revision}
        for number, pin in enumerate(pins, 1):
            key = f'S{number}'
            original = source.track_reading_file(pin['original'])
            original_sha = digest(original.read_bytes())
            resource = pin['resource_id']
            require(all(s['source_sha256'] != original_sha for s in result.values()), 'duplicate source')
            info = source.validate(resource, pin['unit_set_id'])
            manifest, units, sections = source._load_set(resource, pin['unit_set_id'])
            artifact_path = source.track_reading_file(
                f"{ARTIFACT_ROOT}/{resource}/{info['artifact_revision']}/manifest.json")
            artifact, document, _ = source._verify_artifact(artifact_path)
            source._verify_governance(artifact, access)
            bundle_path = source.track_reading_file(pin['bundle_manifest'])
            bundle = _load_json(bundle_path)
            bundle_document = _child_path(bundle_path.parent, bundle['document']['path'], source.vault)
            source.track_reading_file(bundle_document.relative_to(source.vault).as_posix())
            require(
                artifact['source_sha256'] == bundle['source']['sha256'] == original_sha
                and bundle['source']['filename'] == original.name
                and all(bundle['governance'].get(k) == v for k, v in manifest['identity'].items())
                and _normalize_text(bundle_document.read_bytes()) == document,
                'original, Bundle and UnitSet differ',
            )
            section_titles = {s['section_id']: s['title'] for s in sections}
            items, readings, by_unit = {}, [], {}
            for unit in units:
                locator = unit['locator']
                is_asset = locator['kind'] == 'asset'
                item_id = f"{key}-{'A' if is_asset else 'B'}{len(items) + 1}"
                ref = {'unit_ref': unit['ref'], 'span': None}
                read = source.get({'source_ref': ref, 'access': access})
                item = {
                    'kind': 'asset' if is_asset else 'block', 'source_ref': ref,
                    'sha256': read['content_sha256'], 'text': read['core_text'],
                    'section': unit['section_id'], 'heading': [section_titles[h] for h in read['heading_path']],
                    'locator': locator, 'required_assets': [],
                }
                if is_asset and read['core_text'] is None:
                    media = locator['media_type']
                    require(media in IMAGE_TYPES, f'unsupported whole-asset transport: {media}')
                    data = _child_path(artifact_path.parent, locator['path'], source.vault).read_bytes()
                    require(digest(data) == item['sha256'], 'registered asset changed')
                    item['image'] = {'bytes': len(data), 'sha256': item['sha256'], 'media_type': media,
                                     'url': f'data:{media};base64,' + base64.b64encode(data).decode('ascii'),
                                     'references': [{'task': key, 'material': item_id}]}
                items[item_id] = item
                by_unit[unit['ref']['unit_id']] = item_id
                readings.append({'role': 'core', 'source_ref': ref, 'core_text': read['core_text'],
                                 'unit_content_sha256': read['content_sha256']})
            require(
                {(a['path'], a['sha256']) for a in artifact['assets']}
                == {(item['locator']['path'], item['sha256']) for item in items.values()
                    if item['kind'] == 'asset'},
                'UnitSet does not represent every registered whole asset',
            )
            _, supplement = required_reading(source, {
                'materials': readings, 'package_id': info['unit_set_id'], 'task_id': key,
                'actor': access['actor'], 'document_registry_revision': revision,
            })
            for link in supplement['links']:
                parent, asset = (by_unit[link[k]['unit_ref']['unit_id']] for k in ('parent', 'dependency'))
                if asset not in items[parent]['required_assets']:
                    items[parent]['required_assets'].append(asset)
            result[key] = {
                'name': original.name, 'source_sha256': original_sha, 'identity': manifest['identity'],
                'unit_set_id': info['unit_set_id'], 'artifact_revision': info['artifact_revision'],
                'document_sha256': artifact['document_sha256'], 'registry_revision': revision,
                'qa': bundle['quality'], 'bundle_sha256': fingerprint(bundle),
                'outline': [{'id': s['section_id'], 'parent': s['parent_id'], 'title': s['title']} for s in sections],
                'items': items,
            }
    return result


def request(phase, view, schema, output_estimate, limit, images=(), *, reserve=0, slots=False):
    from fixed_semantic import prepare_request_projection

    # Use the same complete message projection as the native semantic path.
    schema = closed({'results': closed(schema if slots else {'r': schema})})
    projection = prepare_request_projection(phase, view, schema, images)
    body = {
        'contract': CONTRACT, 'phase': phase, 'messages': projection['content'],
        'system': projection['wire']['system'], 'schema': schema,
        'transport_schema': projection['transport_schema'], 'output_cap': OUTPUT_LIMIT,
    }
    return {
        'id': fingerprint(body), **body, 'view': view, 'image_inputs': list(images),
        'budget': {
            **projection['request_cost'], 'input_size': projection['input_size'], 'input_cap': limit,
            'input_reserve': reserve, 'estimated_output_tokens': output_estimate,
            'usable_output_tokens': USABLE_OUTPUT,
            'image_bytes': sum(image['bytes'] for image in images),
            'fits': (projection['input_size'] + reserve <= limit and output_estimate <= USABLE_OUTPUT
                     and images_fit(images)),
        },
    }


def seed_requests(sources):
    rows = []
    for key, source in sources.items():
        group = []
        for section in source['outline']:
            trial = [*group, section]
            row = seed_request(key, source, trial)
            if not row['budget']['fits'] and group:
                rows.append(seed_request(key, source, group))
                trial = [section]
            group = trial
        if group:
            row = seed_request(key, source, group)
            require(row['budget']['fits'], f'oversized source outline item: {key}')
            rows.append(row)
    return rows


def seed_request(key, source, sections):
    return request('short-topics', {
        'source': key, 'name': source['name'], 'revision': source_revision(source),
        'sections': sections, 'qa': source['qa'],
    }, SEED_SCHEMA, 480 + 80 * len(sections), PASS_LIMIT)


def source_revision(source):
    return fingerprint({k: source[k] for k in (
        'identity', 'unit_set_id', 'document_sha256', 'source_sha256', 'registry_revision', 'bundle_sha256',
    )})


def evidence(source, selected):
    """Expand transitive explicit asset dependencies without losing primary IDs."""
    items = source['items']
    ordered = dict.fromkeys(selected)
    queue = list(ordered)
    for item_id in queue:
        for dep in items[item_id]['required_assets']:
            if dep not in ordered:
                ordered[dep] = None
                queue.append(dep)
    materials, images = {}, []
    for item_id in ordered:
        item = items[item_id]
        row = {k: item[k] for k in ('kind', 'text', 'heading', 'required_assets')}
        if item['kind'] == 'asset':
            row['label'] = item['locator']['path']
        if 'image' in item:
            images.append(item['image'])
        materials[item_id] = row
    return materials, images


def mapping_request(key, source, assigned, topics, *, context=(), reserve=0, issue=None):
    order = list(source['items'])
    first, last = order.index(assigned[0]), order.index(assigned[-1])
    adjacent = order[max(0, first - 1):first] + order[last + 1:last + 2]
    materials, images = evidence(source, [*assigned, *adjacent, *context])
    view = {'source': key, 'name': source['name'], 'revision': source_revision(source),
            'qa': source['qa'], 'seed_topics': topics,
            'assigned': assigned, 'materials': materials}
    if issue is not None:
        view['issue'] = issue
    # Small per-item records, with room for several relations or a short new topic.
    return request('short-map', view, MAP_SCHEMA, 768 + 144 * len(assigned), PASS_LIMIT, images, reserve=reserve)


def mapping_groups(sources):
    """Fix primary coverage and reserve room for seed output before it exists."""
    result = {}
    for key, source in sources.items():
        groups, group = [], []
        for item_id in source['items']:
            trial = [*group, item_id]
            fits = mapping_request(key, source, trial, [], reserve=SEED_RESERVE)['budget']['fits']
            if group and not fits:
                groups.append(group)
                trial = [item_id]
                fits = mapping_request(key, source, trial, [], reserve=SEED_RESERVE)['budget']['fits']
            require(fits,
                    f'complete material and required assets exceed the mapping budget: {item_id}')
            group = trial
        if group:
            groups.append(group)
        result[key] = groups
    return result


def check_topics(rows):
    ids = {t['id'] for t in rows}
    require(len(ids) == len(rows) and all(t['name'].strip() and t['scope'].strip() for t in rows),
            'duplicate or empty topic')
    return ids


def map_requests(sources, groups, seed_rows, receipts):
    seeds = {key: [] for key in sources}
    for row in seed_rows:
        key = row['view']['source']
        for topic in checked_answer(row, receipts[row['id']])['topics']:
            seeds[key].append({**topic, 'id': f't{len(seeds[key]) + 1}'})
    result = []
    for key, packs in groups.items():
        pending = list(packs)
        while pending:
            items = pending.pop(0)
            row = mapping_request(key, sources[key], items, seeds[key])
            if row['budget']['fits']:
                result.append(row)
                continue
            require(len(items) > 1, f'full item and seed catalogue exceed mapping capacity: {items[0]}')
            midpoint = len(items) // 2
            pending[:0] = [items[:midpoint], items[midpoint:]]
    return result


def accept_map(view, response):
    seeds = check_topics(view['seed_topics'])
    new = check_topics(response['topics'])
    require(not seeds & new, 'map-local topic shadows a seed')
    assigned = set(view['assigned'])
    items = response['dispositions']
    require(len(items) == len(assigned) and {r['item'] for r in items} == assigned,
            'every assigned item needs exactly one disposition')
    for row in items:
        linked = row['status'] == 'linked'
        require(linked == bool(row['links']) and (linked or row['reason'].strip()),
                'linked items need relations and unresolved or excluded items need a reason')
        for link in row['links']:
            require(link['topic'] in seeds | new and set(link['applies_to']) <= view['materials'].keys(),
                    'unknown topic or out-of-packet relation')
            material = view['materials'][row['item']]
            if link['role'] == 'asset':
                require(material['kind'] == 'asset' or material['required_assets'],
                        'asset role requires a whole asset or an explicit registered link')
            elif material['kind'] == 'asset':
                require(False, 'whole asset needs the asset role')


def normalize_map(row, answer):
    """Keep exact native meaning while resolving redundant local presentation."""
    schema = copy.deepcopy(row['schema'])
    fields = schema['properties']['results']['properties']['r']['properties']
    fields['dispositions']['items']['properties']['links']['items']['properties']['note']['maxLength'] = 512
    _check_shape(schema, answer, {}, '$')
    view = row['view']
    result = answer['results']['r']
    seed = {t['id']: t for t in view['seed_topics']}
    assigned = set(view['assigned'])
    require(all(d['item'] in view['materials'] for d in result['dispositions']), 'disposition refers outside the packet')
    return {'results': {'r': {
        'topics': [t for t in result['topics'] if t != seed.get(t['id'])],
        'dispositions': [d for d in result['dispositions'] if d['item'] in assigned],
    }}}


def receipt(row, answer):
    """Seal a verified experimental result. The envelope grants no native authority."""
    require(row['budget']['fits'], 'request does not fit its pinned budget')
    if row['phase'] == 'short-map':
        answer = normalize_map(row, answer)
    else:
        _check_shape(row['schema'], answer, {}, '$')
    response = answer['results']['r']
    if row['phase'] == 'short-topics':
        check_topics(response['topics'])
    elif row['phase'] == 'short-map':
        accept_map(row['view'], response)
    elif row['phase'] == 'short-coordinate':
        from short_evidence_reduce import check_coordination
        check_coordination(row['view'], response)
    elif row['phase'] == 'short-detail' and 'sections' in response:
        from short_evidence_reduce import check_sections
        check_sections(row['view']['page']['topics'], response['sections'])
    body = {'contract': RECEIPT, 'request_sha256': row['id'], 'answer': answer}
    return {**body, 'receipt_sha256': fingerprint(body)}


def checked_answer(row, saved):
    require(receipt(row, saved['answer']) == saved, 'stale or changed experimental receipt')
    return saved['answer']['results']['r']
