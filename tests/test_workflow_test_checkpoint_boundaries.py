"""Capture validators before they execute, never after their own completion."""
from types import SimpleNamespace as NS
import pytest
from hermes_source_units.workflow_test import STAGES
from ingest_kanban import IngestKanbanAdapter

@pytest.mark.parametrize('kind,stage',[('checkpoint-1-validate','reducing'),
    ('checkpoint-2-validate','release_planning'),('checkpoint-2-validate','checkpoint_2')])
def test_checkpoint_capture_stage_matches_native_preexecution_readiness(kind,stage):
    adapter=NS(workflow=NS(revision_allows=lambda *_:False,pending_pause=lambda *_:None))
    workflow={'scope':{'execution_mode':'auto_full'},'dispatch_policy':{'mode':'full'},'current_stage':stage,
        'checkpoints':{'checkpoint_2':{'state':'pending'}}}
    assert IngestKanbanAdapter._canary_allows(adapter,workflow,NS(kind=kind,name=kind))
    assert stage in STAGES[kind]

def test_first_checkpoint_does_not_wait_for_its_own_completion():
    assert 'checkpoint_1' not in STAGES['checkpoint-1-validate']
