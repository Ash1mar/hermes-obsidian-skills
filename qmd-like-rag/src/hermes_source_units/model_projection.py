"""Compact, reversible model views; canonical packages remain authoritative."""
from __future__ import annotations

import copy
import hashlib

from .source_units import _json_bytes, _load_json, _vault_path, _write_atomic
from .validation import ContractError, canonical_json, fingerprint, validate_record

RENDERER = 'bounded-model-input/v1'
BEGIN_ENVELOPE_RESERVE = 2048


def fail(code, message):
    raise ContractError(code, '$', message)


class ModelProjectionMixin:
    def preview_model_input(self, task_id, batch):
        """The same projection before and after claim; no domain writes."""
        task = self._task(task_id)
        package = self._existing_reading_package(task, batch['actor'], batch['document_registry_revision'])
        if package is None:
            assembled = self._assemble_reading(task['target_refs'], batch['actor'],
                batch['document_registry_revision'], self._reader_config())
            package = {'contract':'hermes-reading-package/v1', 'task_id':task_id,
                'task_revision':task['revision'] + (task['status'] == 'pending'),
                'actor':batch['actor'], 'document_registry_revision':batch['document_registry_revision'],
                'window':assembled['window'], 'materials':assembled['materials'],
                'total_codepoints':sum(len(m['core_text'] or '') for m in assembled['materials'])}
            package['package_id'] = fingerprint(package)
        return self.model_input(task_id, package)

    def model_input(self, task_id, package, *, persist=False):
        """Verify all evidence and render it without exposing full identity objects."""
        task = self._task(task_id)
        validate_record('reading_package', package)
        package_hash = self._reading_package_fingerprint(package, package['package_id'])
        if package['task_id'] != task_id or package['window']['core_refs'] != task['target_refs']:
            fail('STALE_INPUT', 'model input does not belong to this task')
        access = {'actor': package['actor'], 'purpose': 'construction',
                  'registry_revision': package['document_registry_revision']}
        headings, heading_ids, mapping, materials, qa_hashes = [], {}, {}, [], {}
        with self.source.reading_snapshot():
            for index, material in enumerate(package['materials'], 1):
                live = self.source.get({'source_ref': material['source_ref'], 'access': access})
                if (live['core_text'] != material['core_text'] or
                        live['content_sha256'] != material['unit_content_sha256'] or
                        live['asset_refs'] != material['asset_refs'] or
                        live['quality_refs'] != material['quality_refs']):
                    fail('SOURCE_CHANGED', 'model material differs from live evidence')
                text = material['core_text']
                selected = hashlib.sha256(text.encode('utf-8')).hexdigest() if text is not None else None
                if selected != material['selected_sha256']:
                    fail('SOURCE_CHANGED', 'selected material hash differs')
                unit = material['source_ref']['unit_ref']
                _, _, sections = self.source._load_set(unit['resource_id'], unit['unit_set_id'])
                titles = {s['section_id']: s['title'] for s in sections}
                heading = tuple(titles.get(h, h) for h in material['heading_path'])
                if heading not in heading_ids:
                    heading_ids[heading] = len(headings)
                    headings.append(list(heading))
                handle = f'm{index}'
                mapping[handle] = copy.deepcopy(material['source_ref'])
                item = {'ref': handle, 'role': material['role'], 'heading': heading_ids[heading], 'text': text}
                if live['locator'].get('span'):
                    item['source_span'] = material['source_ref'].get('span') or live['locator']['span']
                if live['locator']['kind'] == 'asset' and text is None:
                    asset_path = f"_system/sources/artifacts/{unit['resource_id']}/{unit['artifact_revision']}/{live['locator']['path']}"
                    item['asset'] = {'path':asset_path, 'media_type':live['locator']['media_type'],
                                     'permission':'this registered whole asset only'}
                if material['asset_refs']:
                    item['linked_assets'] = [{'name': a['asset_id'], 'type': a['media_type'],
                        'content_provided': False} for a in material['asset_refs']]
                restrictions = []
                for ref in material['quality_refs']:
                    path = _vault_path(self.vault, ref)
                    data = path.read_bytes()
                    qa_hashes[ref] = hashlib.sha256(data).hexdigest()
                    qa = _load_json(path)
                    # Preserve every diagnostic unless its explicit source range
                    # proves it unrelated. Unknown diagnostics stay visible.
                    span = material['source_ref'].get('span') or live['locator'].get('span')
                    for diagnostic in qa.get('diagnostics', [qa]):
                        bound = diagnostic.get('span') if isinstance(diagnostic, dict) else None
                        if (span and isinstance(bound, dict) and type(bound.get('start')) is int
                                and type(bound.get('end')) is int and bound['start'] < bound['end']
                                and (bound['end'] <= span['start'] or bound['start'] >= span['end'])):
                            continue
                        restrictions.append(diagnostic)
                if restrictions:
                    item['qa_restrictions'] = restrictions
                if material['quality_refs']:
                    item['qa_qualification'] = 'Review source QA restrictions before treating affected claims as authoritative.'
                materials.append(item)
        view = {'headings': headings, 'materials': materials, 'limits': {
            'context_truncated': package['window']['truncated'],
            'omitted_material_count': len(package['window']['omitted_refs']),
            'reason': package['window']['reason']}}
        identity = {'renderer': RENDERER, 'task_id': task_id,
                    'package_id': package['package_id'], 'package_hash': package_hash,
                    'qa_hashes': qa_hashes, 'view': view, 'mapping': mapping}
        input_id = fingerprint(identity)
        # Historical Passes are context, not new evidence. Resolve them through
        # the same bounded mapping, never include hashes/full UnitRefs in the view.
        root = _vault_path(self.vault, f'_system/knowledge-builds/task-{task_id}/passes')
        records = [_load_json(p) for p in sorted(root.glob('*.json'))] if root.exists() else []
        previous = []
        for record in records:
            validate_record('knowledge_pass', record)
            if fingerprint({k:v for k,v in record.items() if k != 'pass_id'}) != record['pass_id']:
                fail('SOURCE_CHANGED', 'historical Pass fingerprint changed')
            if record['reading_package_id'] != package['package_id']:
                fail('STALE_INPUT', 'existing Pass uses a different reading package')
            previous.append({'sequence': record['sequence'], 'pass_kind': record['pass_kind'],
                'inspections': [{**i, 'source_ref': self._model_handle(i['source_ref'], mapping)}
                                for i in record['inspections']],
                'candidates': [{**c, 'support_refs': [self._model_handle(r, mapping) for r in c['support_refs']]}
                               for c in record['candidates']], 'empty_reason': record['empty_reason']})
        sequences = [p['sequence'] for p in previous]
        if sequences != list(range(len(sequences))):
            fail('INVALID_PASS', 'existing Pass sequence is not contiguous')
        view.update(input_id=input_id, next_sequence=len(previous))
        if previous:
            view['previous_passes'] = previous
        # Also hash the actual presentation, including resumable prior results.
        manifest = {**identity, 'input_id': input_id, 'view_hash': fingerprint(view)}
        ref = f'_system/knowledge-builds/task-{task_id}/model-inputs/{input_id}-{manifest["view_hash"]}.json'
        if persist:
            path = _vault_path(self.vault, ref)
            if path.exists() and _load_json(path) != view:
                fail('IDEMPOTENCY_CONFLICT', 'model projection identity has different content')
            if not path.exists():
                _write_atomic(path, canonical_json(view) + b'\n')
            manifest_path = path.with_suffix('.manifest.json')
            if manifest_path.exists() and _load_json(manifest_path) != manifest:
                fail('IDEMPOTENCY_CONFLICT', 'model mapping identity has different content')
            if not manifest_path.exists():
                _write_atomic(manifest_path, _json_bytes(manifest))
        return {'input_id': input_id, 'view': view, 'mapping': mapping,
                'codepoints': len(canonical_json(view).decode('utf-8')) + 1, 'path': ref}

    @staticmethod
    def _model_handle(ref, mapping):
        for handle, original in mapping.items():
            if ref == original:
                return handle
            if ref['unit_ref'] == original['unit_ref'] and ref.get('span'):
                span = original.get('span')
                if span is None or (span['start'] <= ref['span']['start'] < ref['span']['end'] <= span['end']):
                    return {'ref': handle, 'span': ref['span']}
        fail('OUTSIDE_READING_PACKAGE', 'historical citation is outside current material')

    @staticmethod
    def _expand_model_ref(value, mapping):
        handle = value if isinstance(value, str) else value.get('ref') if isinstance(value, dict) else None
        if handle not in mapping:
            fail('OUTSIDE_READING_PACKAGE', 'unknown model evidence handle')
        original = copy.deepcopy(mapping[handle])
        if isinstance(value, dict):
            if set(value) != {'ref', 'span'} or not isinstance(value['span'], dict):
                fail('INVALID_SCHEMA', 'compact reference permits only ref and span')
            span = value['span']
            if (set(span) != {'start', 'end'} or type(span['start']) is not int or
                    type(span['end']) is not int or span['start'] < 0 or span['end'] <= span['start']):
                fail('INVALID_RANGE', 'invalid compact citation range')
            bounded = original.get('span')
            if bounded and not bounded['start'] <= span['start'] < span['end'] <= bounded['end']:
                fail('OUTSIDE_READING_PACKAGE', 'compact reference expands beyond selected span')
            original['span'] = dict(span)
        return original

    def expand_model_passes(self, request, task_ids, actor, batch_id):
        """Expand a model-authored draft; never invent semantic fields."""
        if set(request) != {'passes'} or not isinstance(request['passes'], list) or not request['passes']:
            fail('INVALID_SCHEMA', 'compact draft requires a nonempty passes array')
        aliases = {f't{i}': tid for i, tid in enumerate(task_ids, 1)}
        expanded, seen = [], set()
        for draft in request['passes']:
            allowed = {'task', 'input_id', 'sequence', 'inspections', 'candidates', 'empty_reason'}
            if set(draft) != allowed or draft['task'] not in aliases or draft['task'] in seen:
                fail('INVALID_SCHEMA', 'compact task must be unique and inside the bound slice')
            seen.add(draft['task'])
            tid = aliases[draft['task']]
            task = self._task(tid)
            package = self._existing_reading_package(task, actor, self._batch(batch_id)['document_registry_revision'])
            if package is None:
                fail('STALE_INPUT', 'task has no prepared reading package')
            projection = self.model_input(tid, package)
            if draft['input_id'] != projection['input_id']:
                fail('STALE_INPUT', 'compact input identity changed')
            sequence = draft['sequence']
            if type(sequence) is not int or sequence < 0 or sequence > projection['view']['next_sequence']:
                fail('INVALID_PASS', 'compact sequence is not the next or an existing Pass')
            mapping = projection['mapping']
            inspections = []
            for item in draft['inspections']:
                if set(item) != {'source_ref', 'finding', 'qa', 'qa_note'}:
                    fail('INVALID_SCHEMA', 'compact inspection has unexpected fields')
                inspections.append({**item, 'source_ref': self._expand_model_ref(item['source_ref'], mapping)})
            candidates = []
            fields = {'candidate_id', 'name', 'kind', 'identity_rationale', 'finding', 'applicability',
                      'conditions', 'exceptions', 'support_refs'}
            for item in draft['candidates']:
                if set(item) != fields:
                    fail('INVALID_SCHEMA', 'compact candidate has unexpected fields')
                candidates.append({**item, 'support_refs': [self._expand_model_ref(r, mapping) for r in item['support_refs']]})
            root = _vault_path(self.vault, f'_system/knowledge-builds/task-{tid}/passes')
            prior = list(root.glob(f'{sequence:04d}-*.json')) if root.exists() else []
            expected = _load_json(prior[0])['task_revision'] if len(prior) == 1 else task['revision']
            expanded.append({'task_id': tid, 'actor': actor, 'expected_revision': expected,
                'registry_revision': package['document_registry_revision'], 'reading_package_id': package['package_id'],
                'pass_kind': 'candidate' if sequence == 0 else 'citation', 'sequence': sequence,
                'inspections': inspections, 'candidates': candidates, 'empty_reason': draft['empty_reason']})
        return {'batch_id': batch_id, 'passes': expanded}
