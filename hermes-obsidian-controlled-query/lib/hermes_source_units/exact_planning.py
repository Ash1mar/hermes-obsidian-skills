"""Budget-based planning with validated, restartable measurement checkpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .source_units import ARTIFACT_ROOT, _json_bytes, _load_json, _vault_path, _write_atomic
from .validation import ContractError, fingerprint, validate_record


class ExactPlanner:
    """One bounded planning session. Cache reuse requires fresh source/ACL checks."""

    def __init__(self, service, request, check=None):
        self.service, self.request = service, dict(request)
        self.check = check or (lambda: None)
        self.actor = str(request['actor'])
        self.registry = int(request['registry_revision'])
        self.reader = service._reader_config(request.get('max_codepoints'))
        self.snapshots = {}
        self.reused = self.measured = 0
        self.tasks = []
        self.source = service.source
        self.source.enable_session_cache()
        refs = []
        for item in request['sources']:
            key = (str(item['resource_id']), str(item['unit_set_id']))
            if key in self.snapshots:
                raise ContractError('DUPLICATE', '$.sources', 'duplicate source UnitSet')
            self.check()
            self.source.validate(*key)
            manifest, units, sections = self.source._load_set(*key)
            if not units:
                raise ContractError('INCOMPLETE_COVERAGE', '$.sources', 'source UnitSet is empty')
            digest = fingerprint({'manifest':manifest, 'units':units, 'sections':sections})
            self.snapshots[key] = (manifest, units, digest)
            refs.extend({'unit_ref':unit['ref'], 'span':None} for unit in units)
        if not refs:
            raise ContractError('INCOMPLETE_COVERAGE', '$.sources', 'no ready source units')
        self.refs = refs
        self.session = fingerprint({'algorithm':'exact-planner/v1', 'actor':self.actor,
            'registry_revision':self.registry, 'reader':self.reader,
            'sources':[[list(key), value[2]] for key,value in sorted(self.snapshots.items())],
            'batch_id':request['batch_id'], 'scope':request.get('scope_fingerprint')})
        self.root = _vault_path(service.vault, f'_system/reports/exact-planning/{self.session}')

    def _live(self, refs):
        self.check()
        seen = set()
        for ref in refs:
            unit = ref['unit_ref']
            key = (unit['resource_id'], unit['unit_set_id'])
            if key in seen:
                continue
            seen.add(key)
            manifest, units, _ = self.source._load_set(*key)
            pinned = self.snapshots.get(key)
            if pinned is None or manifest is not pinned[0] or units is not pinned[1]:
                raise ContractError('STALE_PLAN', '$', 'UnitSet changed during exact planning')
            # Includes current registry revision, actor ACL and artifact byte checks.
            self.source.get({'source_ref':ref, 'access':{'actor':self.actor,
                'purpose':'construction', 'registry_revision':self.registry}})

    def measure(self, refs):
        self._live(refs)
        revisions = self.service._unitset_revisions(refs)
        reader_hash = fingerprint(self.reader)
        expected = 'sha256:' + fingerprint({'unit_refs':refs,
            'registry_revision':self.registry, 'unitset_revisions':revisions,
            'reader_config_hash':reader_hash})
        key = fingerprint({'session':self.session, 'refs':refs})
        path = self.root / 'measurements' / (key + '.json')
        if path.is_file():
            try:
                saved = _load_json(path)
                value = saved['measurement']
                validate_record('reading_window_measurement', value)
                if (saved['session'] == self.session and saved['refs'] == refs
                        and saved['measurement_hash'] == fingerprint(value)
                        and value['input_fingerprint'] == expected
                        and value['reader_config_hash'] == reader_hash
                        and value['unitset_revisions'] == revisions
                        and value['limit'] == self.reader['max_codepoints']
                        and value['fits'] == (value['serialized_codepoints'] <= value['limit'])):
                    self.reused += 1
                    return value
            except (ContractError, KeyError, ValueError, TypeError):
                pass  # incomplete/corrupt checkpoints are recomputed, never trusted
        value = self.service.measure_reading_window(refs, self.registry, revisions,
                                                    self.reader, actor=self.actor)
        self._live(refs)
        self._store(path, {'session':self.session, 'refs':refs,
            'measurement':value, 'measurement_hash':fingerprint(value)})
        self.measured += 1
        return value

    def _store(self, path, value):
        from .workflow_guard import workflow_write_guard
        self.check()
        with workflow_write_guard(self.service.vault, kinds=('exact-plan',), actor=self.actor):
            _write_atomic(path, _json_bytes(value))

    def _progress(self, state, **extra):
        self.check()
        value = {'contract':'hermes-exact-planning-progress/v1', 'session':self.session,
            'state':state, 'total_units':len(self.refs),
            'accepted_units':sum(len(task['target_refs']) for task in self.tasks),
            'task_count':len(self.tasks), 'new_measurements':self.measured,
            'reused_measurements':self.reused, **extra}
        self._store(self.root / 'progress.json', value)

    def prepare(self):
        """Deterministic initial windows, recursively split on actual serialized budget."""
        self._progress('measuring')
        def split(refs):
            measurement = self.measure(refs)
            if measurement['fits']:
                task_id = 'exact-' + fingerprint({'batch':self.request['batch_id'], 'refs':refs})[:32]
                self.tasks.append({'task_id':task_id, 'target_refs':refs, 'expected_revision':0})
                self._progress('measuring')
            elif len(refs) > 1:
                middle = len(refs)//2
                split(refs[:middle])
                split(refs[middle:])
            else:
                self._progress('blocked', blocking_code=measurement['blocking_code'],
                               measurement=measurement, source_ref=refs[0])
                raise ContractError(str(measurement['blocking_code']), '$',
                    'one indivisible SourceUnit exceeds serialized budget; inspect exact-planning progress')
        # Never mix documents in a candidate window; preserve each source's order.
        for key, (_,units,_) in self.snapshots.items():
            refs = [{'unit_ref':unit['ref'], 'span':None} for unit in units]
            for start in range(0,len(refs),8):
                split(refs[start:start+8])
        plan = {'batch_id':self.request['batch_id'], 'actor':self.actor,
                'registry_revision':self.registry, 'tasks':self.tasks,
                'max_codepoints':self.reader['max_codepoints'], 'exact_reading_budget':True,
                'origin':self.request.get('origin', {'kind':'generated'})}
        self._live(self.refs)
        self._store(self.root / 'plan-request.json', plan)
        self._progress('measured', plan_fingerprint=fingerprint(plan))
        return plan
