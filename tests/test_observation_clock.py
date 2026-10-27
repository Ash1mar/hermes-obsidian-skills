"""Clock discontinuities are isolated fixtures, never injected into a real workflow."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-source-units/src'))
from hermes_source_units import runtime_telemetry as telemetry
import pytest


def sample(wall,mono,domain='isolated'):
    return {'wall_time_ns':wall*1_000_000_000,'monotonic_ns':mono*1_000_000_000,'clock_domain':domain}


def test_span_duration_uses_monotonic_and_retains_wall_discontinuity(tmp_path,monkeypatch):
    samples=iter([sample(100,1),sample(90,3)])
    monkeypatch.setattr(telemetry,'clock_sample',lambda:next(samples))
    monkeypatch.setattr(telemetry,'runtime_directory',lambda *_:tmp_path)
    with telemetry.span('clock-fixture',vault=tmp_path,workflow_id='isolated'):pass
    row=json.loads(next(tmp_path.glob('*.jsonl')).read_text())
    assert row['finished_at']-row['started_at']==-10
    assert row['finished_monotonic_ns']-row['started_monotonic_ns']==2_000_000_000
    assert row['elapsed_ms']==2000 and row['clock_domain']=='isolated'


def test_observer_deadline_ignores_wall_jumps_and_cannot_reset_domain():
    deadline=telemetry.monotonic_deadline(sample(100,10),180)
    assert telemetry.remaining_ns(deadline,sample(10000,189))==1_000_000_000
    assert telemetry.remaining_ns(deadline,sample(-10000,190))==0
    assert telemetry.remaining_ns(deadline,sample(0,191))==0
    with pytest.raises(ValueError,match='clock domain changed'):
        telemetry.remaining_ns(deadline,sample(100,11,'different-boot'))
    with pytest.raises(ValueError):telemetry.monotonic_deadline(sample(100,10),0)
