"""Later-ready sources keep mapping while the one current round delivers."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
import incremental_ingest_controller as controller
import incremental_runtime as runtime
from hermes_source_units.incremental_qualification import QualificationContention, qualification_lock
from hermes_source_units.source_units import _exclusive_lock


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
    started, checkpoints = [], []
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
    assert checkpoints == [('mapping', 'b'), ('closure', 'run')]
    assert not (tmp_path / 'native').exists()
