"""Later-ready sources keep mapping while the one current round delivers."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
import incremental_ingest_controller as controller


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
