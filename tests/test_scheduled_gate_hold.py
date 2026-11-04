"""A scheduled gate remains held across cancellation/resume without reblocking."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from ingest_kanban import KanbanCLI


def test_scheduled_gate_hold_never_invokes_illegal_native_transition():
    cli=KanbanCLI()
    cli._projection_board='board'
    cli._projection_rows={'gate':{'status':'scheduled','assignee':'default'}}
    def forbidden(*a):raise AssertionError('scheduled gate is already held')
    cli.command=forbidden
    cli.wait('board','gate','blocked','Vault gate or outside active dispatch scope')
    assert cli._projection_rows['gate']['status']=='scheduled'
