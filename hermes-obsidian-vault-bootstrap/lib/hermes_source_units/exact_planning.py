"""Budget-based planning with validated, restartable measurement checkpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .source_units import ARTIFACT_ROOT, _json_bytes, _load_json, _vault_path, _write_atomic
from .validation import ContractError, fingerprint, validate_record


class ExactPlanner:
    """One bounded planning session. Cache reuse requires fresh source/ACL checks."""

    def __init__(self, service, request, check=None, *, model_measure=None, read_only=False):
        self.service, self.request = service, dict(request)
        self.check = check or (lambda: None)
        self.actor = str(request['actor'])
        self.registry = int(request['registry_revision'])
        self.reader = service._reader_config(request.get('max_codepoints'))
        self.model_measure = model_measure
        self.read_only = read_only
        self.budget_identity = getattr(model_measure, 'identity', None)
        self.budgets = {}
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
        self.session = fingerprint({'algorithm':'sequential-exact-planner/v2', 'actor':self.actor,
            'registry_revision':self.registry, 'reader':self.reader,
            'model_budget':self.budget_identity,
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
        # One assembly supplies canonical size and the actual runtime projection.
        # Fresh projection checks also detect changed QA/required assets before
        # a checkpoint can be reused. No model call or domain output is produced.
        assembled = self.service._assemble_reading(refs, self.actor, self.registry, self.reader)
        value = assembled['measurement']
        budget = self.model_measure(refs, assembled) if self.model_measure and value['fits'] else None
        self.budgets[key] = budget
        if not self.read_only and path.is_file():
            try:
                saved = _load_json(path)
                saved_value = saved['measurement']
                validate_record('reading_window_measurement', saved_value)
                if (saved['session'] == self.session and saved['refs'] == refs
                        and saved['measurement_hash'] == fingerprint(saved_value)
                        and saved_value == value and saved.get('model_budget') == budget
                        and saved_value['input_fingerprint'] == expected
                        and saved_value['reader_config_hash'] == reader_hash
                        and saved_value['unitset_revisions'] == revisions):
                    self.reused += 1
                    return value
            except (ContractError, KeyError, ValueError, TypeError):
                pass  # incomplete/corrupt checkpoints are recomputed, never trusted
        self._live(refs)
        self._store(path, {'session':self.session, 'refs':refs,
            'measurement':value, 'measurement_hash':fingerprint(value), 'model_budget':budget})
        self.measured += 1
        return value

    def _store(self, path, value):
        from .workflow_guard import workflow_write_guard
        self.check()
        if self.read_only:
            return
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

    def fits(self, refs):
        measurement = self.measure(refs)
        budget = self.budgets[fingerprint({'session':self.session, 'refs':refs})]
        return measurement['fits'] and (budget is None or budget['fits']), measurement, budget

    def prepare(self):
        """Accumulate whole Units in source order; close before the next exceeds budget."""
        self._progress('measuring')
        def accept(refs):
            task_id = 'exact-' + fingerprint({'batch':self.request['batch_id'], 'refs':refs})[:32]
            self.tasks.append({'task_id':task_id, 'target_refs':refs, 'expected_revision':0})
            self._progress('measuring')
        # Never mix documents in a candidate window; preserve each source's order.
        for key, (_,units,_) in self.snapshots.items():
            refs = [{'unit_ref':unit['ref'], 'span':None} for unit in units]
            current = []
            for ref in refs:
                trial = [*current, ref]
                fits, measurement, budget = self.fits(trial)
                if not fits and current:
                    accept(current)
                    current = []
                    trial = [ref]
                    fits, measurement, budget = self.fits(trial)
                if not fits:
                    code = measurement['blocking_code'] or budget['blocking_code']
                    self._progress('blocked', blocking_code=code, measurement=measurement,
                                   model_budget=budget, source_ref=ref)
                    raise ContractError(str(code), '$', 'one indivisible SourceUnit exceeds budget; no truncation')
                current = trial
            if current:
                accept(current)
        plan = {'batch_id':self.request['batch_id'], 'actor':self.actor,
                'registry_revision':self.registry, 'tasks':self.tasks,
                'max_codepoints':self.reader['max_codepoints'], 'exact_reading_budget':True,
                'origin':self.request.get('origin', {'kind':'generated'})}
        self._live(self.refs)
        self._store(self.root / 'plan-request.json', plan)
        self._progress('measured', plan_fingerprint=fingerprint(plan))
        return plan
