"""Observed stage budget, never an upgrade/migration or a new allowance."""
from hermes_source_units.input_budget import *
from hermes_source_units.source_units import _load_json

def observed(adapter,binding):
    path=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/presentation-contract.json'
    if not path.exists():return None
    row=_load_json(path)
    if row.get('input_budget_contract')==CONTRACT and not row.get('input_budget'):fail('pinned stage budget is missing')
    return validate(row['input_budget']) if row.get('input_budget') else None

def bound(adapter,binding,phase,default=30000):
    row=observed(adapter,binding)
    return row['phases'][phase]['input_codepoints'] if row else default
