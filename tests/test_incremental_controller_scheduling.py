"""Later-ready sources keep mapping while the one current round delivers."""
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import time
import json

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts'))
import incremental_ingest_controller as controller
import incremental_runtime as runtime
from hermes_source_units.incremental_qualification import QualificationContention, qualification_lock
from hermes_source_units.source_units import _exclusive_lock
from hermes_source_units.validation import ContractError


class InlineExecutor:
    def __init__(self, *, max_workers):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future


@pytest.mark.parametrize('stage', ['mapping', 'coordination'])
@pytest.mark.parametrize('latch,code,paused', [('pausing', 'INCREMENTAL_PAUSED', True),
    ('active', 'INCREMENTAL_PAUSED', False), ('pausing', 'STALE_INPUT', False)])
def test_watch_finishes_durable_pause_only_for_a_bound_admission_failure(tmp_path, monkeypatch, stage, latch, code, paused):
    import orchestration

    round_id = 'round-1' if stage == 'coordination' else None
    run = {'state': 'active', 'sources': {} if round_id else {'a': {'state': 'prepared', 'snapshot_id': 'a'}},
           'active_round_id': round_id, 'map_result_ids': [], 'consumed_map_ids': [],
           'round_ids': [round_id] if round_id else []}
    head = {'state': 'active', 'boundary': None}
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, status=lambda _: run)
    events = []

    class Control:
        def __init__(self, *_):
            pass

        def status(self):
            return head

        def reconcile_executors(self):
            pass

        def checkpoint(self, scope, checkpoint, evidence):
            assert scope == {'scope_kind': 'run', 'scope_id': 'run'}
            assert head['boundary'] == {'stage': 'manual'}
            events.append('paused')
            head['state'] = 'paused'
            return head

    def sync(*_):
        events.append('sync')
        return run

    def interrupted(*_):
        events.append(stage)
        head.update(state=latch, boundary={'stage': 'manual'} if latch == 'pausing' else None)
        raise ContractError(code, '$', 'fixture admission changed')

    monkeypatch.setattr(controller, 'service', lambda *_: domain)
    monkeypatch.setattr(controller, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'ThreadPoolExecutor', InlineExecutor)
    monkeypatch.setattr(controller, 'sync_sources', sync)
    monkeypatch.setattr(controller, 'map_source' if stage == 'mapping' else 'round_work', interrupted)
    monkeypatch.setattr(controller, 'time', SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(orchestration, 'configuration', lambda: {'source_prepare_concurrency': 1})
    if paused:
        assert controller.watch(tmp_path, 'run') == run
        assert events == ['sync', stage, 'paused'] and run['active_round_id'] == round_id
    else:
        with pytest.raises(ContractError) as failure:
            controller.watch(tmp_path, 'run')
        assert failure.value.code == code and events == ['sync', stage]


def test_watch_reviews_each_eligible_historical_mapping_hold_at_most_once(tmp_path, monkeypatch):
    import incremental_runtime
    import orchestration

    run = {'state': 'active', 'active_round_id': None, 'map_result_ids': [], 'consumed_map_ids': [],
           'sources': {key: {'state': 'mapping_held', 'snapshot_id': key} for key in ('eligible', 'unrelated')}}
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, status=lambda _: run)
    events = []

    class Control:
        def __init__(self, *_):
            pass

        def status(self):
            return {'state': 'active'}

        def reconcile_executors(self):
            pass

    def close(*_):
        events.append('closure')
        run['state'] = 'partial'
        return run

    monkeypatch.setattr(controller, 'service', lambda *_: domain)
    monkeypatch.setattr(controller, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'ThreadPoolExecutor', InlineExecutor)
    monkeypatch.setattr(controller, 'sync_sources', lambda *_: run)
    monkeypatch.setattr(controller, 'map_source', lambda _, __, snapshot: events.append(snapshot))
    monkeypatch.setattr(controller, 'close', close)
    monkeypatch.setattr(controller, 'time', SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(incremental_runtime, 'resumable_mapping_hold', lambda _, __, source: source['snapshot_id'] == 'eligible')
    monkeypatch.setattr(orchestration, 'configuration', lambda: {'source_prepare_concurrency': 1})
    assert controller.watch(tmp_path, 'run')['state'] == 'partial'
    assert events == ['eligible', 'closure']


@pytest.mark.parametrize('control_state', ['active', 'paused', 'cancelled'])
def test_node_delivery_releases_lock_then_reattaches_only_if_still_active(tmp_path, monkeypatch, control_state):
    import manage_incremental_ingest as command

    run = {'state': 'active', 'run_id': 'run', 'actor': 'agent', 'revision': 3, 'active_round_id': 'round-1'}
    head = {'state': 'active', 'revision': 4, 'pause_after': [], 'released': []}
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, status=lambda _: run)
    lock = tmp_path / '.controller.lock'
    folder = tmp_path / 'rounds/round-1'
    folder.mkdir(parents=True)
    (folder/'delivery.json').write_text(json.dumps({'state': 'release_applying'}))
    request = tmp_path/'recover.json'
    request.write_text(json.dumps({'run_id': 'run', 'round_id': 'round-1', 'expected_revision': 3,
        'actor': 'agent', 'operation_id': 'resume', 'disposition': 'continue', 'control_revision': 4,
        'stop_after': 'index_verified'}))
    events = []

    class Control:
        def __init__(self, *_): pass
        def status(self): return head
        def reconcile_executors(self): pass
        def checkpoint(self, *_): return head

    def recover(*_, **options):
        assert options['stop_after'] == 'index_verified' and not options['settle_terminal']
        events.append('native receipt')
        head['state'] = control_state
        return {'state': 'index_verified', 'release_id': 'release', 'release_hash': 'hash',
                'provider': {'index_generation': 'generation'}}

    def spawn(*_):
        with _exclusive_lock(lock, timeout=0, marker=False):
            events.append('controller')
        return {'pid': 1}

    monkeypatch.setattr(sys, 'argv', ['manage', '--vault', str(tmp_path), 'recover-delivery', '--request', str(request)])
    monkeypatch.setattr(command, 'service', lambda *_: domain)
    monkeypatch.setattr(command, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'recover_delivery', recover)
    monkeypatch.setattr(controller, 'attach_sources', lambda *_: events.append('attach sources'))
    monkeypatch.setattr(controller, 'spawn', spawn)
    assert command.main() == 0
    assert events == (['native receipt', 'attach sources', 'controller'] if control_state == 'active' else ['native receipt'])


def test_resume_restores_unobserved_mapping_checkpoint_before_dispatch(tmp_path, monkeypatch):
    import orchestration

    run = {'state': 'active', 'map_result_ids': ['map-b'], 'consumed_map_ids': [],
           'sources': {'b': {'state': 'mapped', 'snapshot_id': 'b', 'map_result_id': 'map-b'}}}
    head = {'state': 'active', 'pause_after': ['mapping'], 'released': []}
    verified = []

    def read_map(run_id, map_id):
        verified.append((run_id, map_id))
        return {'snapshot_id': 'b'}

    class Control:
        def __init__(self, *_): pass
        def reconcile_executors(self): pass
        def status(self): return head

        def checkpoint(self, scope, stage, evidence):
            assert verified == [('run', 'map-b')]
            assert (scope, stage, evidence) == ({'scope_kind': 'source', 'scope_id': 'b'},
                                                'mapping', {'snapshot_id': 'b'})
            head['state'] = 'paused'
            return head

    def premature_dispatch(*_):
        raise AssertionError('restored checkpoint must precede dispatch')

    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, status=lambda _: run, read_map=read_map)
    monkeypatch.setattr(controller, 'service', lambda *_: domain)
    monkeypatch.setattr(controller, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'sync_sources', premature_dispatch)
    monkeypatch.setattr(orchestration, 'configuration', lambda: {'source_prepare_concurrency': 2})
    assert controller.watch(tmp_path, 'run') == run
    assert head['state'] == 'paused'


def test_source_ready_during_round_can_map_before_round_finishes(tmp_path,monkeypatch):
    import orchestration
    round_started=Event()
    b_mapped=Event()
    state={'state':'active','sources':{'a':{'state':'mapped','snapshot_id':'a'},'b':{'state':'pending'}},
           'active_round_id':'round-1','map_result_ids':['map-a'],'consumed_map_ids':['map-a'],'round_ids':['round-1']}
    domain=SimpleNamespace(vault=tmp_path,root=lambda _:tmp_path,status=lambda _:state)
    class Control:
        def __init__(self,*_):pass
        def reconcile_executors(self):pass
        def status(self):return {'state':'active'}
        def checkpoint(self,*_):return {'state':'active'}
    def sync(*_):
        if round_started.is_set() and state['sources']['b']['state']=='pending':
            state['sources']['b']={'state':'prepared','snapshot_id':'b'}
        return state
    def map_b(*_):
        state['sources']['b']['state']='mapped'
        b_mapped.set()
    def work(*_):
        round_started.set()
        assert b_mapped.wait(10),'later-ready source was blocked by current round'
        state['active_round_id']=None
    def close(*_):
        assert b_mapped.is_set()
        state['state']='completed'
        return state
    monkeypatch.setattr(controller,'service',lambda *_:domain)
    monkeypatch.setattr(controller,'IncrementalControl',Control)
    monkeypatch.setattr(controller,'sync_sources',sync)
    monkeypatch.setattr(controller,'map_source',map_b)
    monkeypatch.setattr(controller,'round_work',work)
    monkeypatch.setattr(controller,'close',close)
    monkeypatch.setattr(orchestration,'configuration',lambda:{'source_prepare_concurrency':2})
    assert controller.watch(tmp_path,'run')['state']=='completed'


def test_qualification_backoff_frees_capacity_honors_pause_and_holds_only_one_source(tmp_path, monkeypatch):
    import orchestration

    run = {'state': 'active', 'sources': {key: {'state': 'prepared', 'snapshot_id': key} for key in ('a', 'b')},
           'active_round_id': None, 'map_result_ids': [], 'consumed_map_ids': [], 'round_ids': []}
    head = {'state': 'active', 'pause_after': []}
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path, status=lambda _: run)
    started, checkpoints, closures = [], [], []
    clock = SimpleNamespace(now=0)
    lock = tmp_path / 'origin.lock'

    def sleep(seconds):
        clock.now += seconds / 30
        time.sleep(.001)

    class Control:
        def __init__(self, *_): pass
        def reconcile_executors(self): pass
        def status(self): return head

        def checkpoint(self, scope, stage, evidence):
            checkpoints.append((stage, scope['scope_id']))
            if stage == 'mapping' and scope['scope_id'] == 'b':
                head['state'] = 'paused'
            return head

    def map_one(_vault, rid, snapshot):
        started.append(snapshot)
        if snapshot == 'a':
            try:
                with qualification_lock(lock, time.monotonic() + .05, lambda: None):
                    raise AssertionError('fixture lock was released before the qualification wait expired')
            except QualificationContention as error:
                runtime.record_qualification_wait(domain, rid, snapshot, error)
                return runtime.MAPPING_DEFERRED
        run['sources'][snapshot]['state'] = 'mapped'

    def hold(_domain, _run_id, method, _operation_id, **fields):
        assert method == 'hold_source_mapping' and fields['source_path'] == fields['snapshot_id'] == 'a'
        assert len(runtime.qualification_wait(tmp_path, 'a')['attempts']) == 3
        run['sources']['a']['state'] = 'mapping_held'

    def close(*_):
        closures.append(run['sources']['a']['state'])
        run['state'] = 'partial'
        return run

    monkeypatch.setattr(controller, 'service', lambda *_: domain)
    monkeypatch.setattr(controller, 'IncrementalControl', Control)
    monkeypatch.setattr(controller, 'sync_sources', lambda *_: run)
    monkeypatch.setattr(controller, 'map_source', map_one)
    monkeypatch.setattr(controller, 'mutate', hold)
    monkeypatch.setattr(controller, 'close', close)
    monkeypatch.setattr(controller, 'time', SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(orchestration, 'configuration', lambda: {'source_prepare_concurrency': 1})
    with _exclusive_lock(lock):
        controller.watch(tmp_path, 'run')
        assert started == ['a', 'b'] and checkpoints == [('mapping', 'b')]
        clock.now += 10
        controller.watch(tmp_path, 'run')  # No retry even after the timer elapsed under a pause.
        assert started == ['a', 'b']
        head['state'] = 'active'
        assert controller.watch(tmp_path, 'run')['state'] == 'partial'
    assert started == ['a', 'b', 'a', 'a']
    assert checkpoints == [('mapping', 'b')] and closures == ['mapping_held']
    assert not (tmp_path / 'native').exists()
