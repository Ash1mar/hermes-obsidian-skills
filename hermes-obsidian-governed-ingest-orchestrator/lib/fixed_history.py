"""Fresh recovery bytes with operation-local scope/node projections, never grants."""
from hermes_source_units import ContractError
from hermes_source_units.source_units import _vault_path
from hermes_source_units.verified_json import VerifiedJsonReads


def entries(adapter,binding,*,scopes=None,reverse=False,select=None,query=None,
            code='STALE_INPUT',message='recovery evidence changed'):
    workflow=adapter.workflow.status(binding['workflow_id'])
    cache=getattr(adapter.workflow,'_verified_json_reads',None) or VerifiedJsonReads()
    allowed=tuple(scopes) if scopes is not None else None
    def project(report):
        if allowed is not None and report['request'].get('recovery_scope') not in allowed:return []
        found=[e for e in report['entries'] if e['node']==binding['node']]
        return [selected for e in found if (selected:=select(e) if select else e) is not None]
    pointers=workflow.get('execution_recovery_history',[])
    for pointer in reversed(pointers) if reverse else pointers:
        path=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        try:
            rows=cache.read(path,expected=pointer['report_sha256'],project=project,
                projection_key=('recovery_entries',allowed,binding['node'],query)
                    if select is None or query is not None else None)
        except ContractError as exc:
            if exc.code=='SOURCE_CHANGED':raise ContractError(code,'$',message) from exc
            raise
        for entry in rows:yield pointer,entry
