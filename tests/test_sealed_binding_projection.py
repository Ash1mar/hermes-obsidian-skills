from hermes_source_units.evidence_snapshot import binding
from hermes_source_units.verified_json import VerifiedJsonReads
import json


def test_sealed_binding_selects_fingerprint_without_recopying_revision(tmp_path):
    value={'workflow_id':'w','batch_id':'b','pass_revision':{'tasks':[{'large':'x'*1000}]*100},
           'execution_recovery_history':[{'report_ref':'r'}],'cancel_requested':False}
    path=tmp_path/'w.json';path.write_text(json.dumps(value));cache=VerifiedJsonReads()
    snap=cache.read(path,readonly=True)
    assert binding(snap)==binding(value)==binding(snap)
    assert not any('[pass_revision]' in origin for origin in cache.copy_origins)
    # Dynamic stop bits are intentionally observed by the original workflow
    # gates, not folded into this static-evidence-only identity.
    value['cancel_requested']=True;assert binding(snap)==binding(value)
    value['execution_recovery_history'].append({'report_ref':'changed'})
    assert binding(snap)!=binding(value)
