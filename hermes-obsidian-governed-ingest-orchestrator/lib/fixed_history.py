"""Fresh recovery bytes with operation-local scope/node projections, never grants."""
from hermes_source_units import ContractError
from hermes_source_units.source_units import _vault_path
from hermes_source_units.verified_json import VerifiedJsonReads


def checked_report(service,pointer):
    """Hash and parse the same captured report, rather than reopen after hashing."""
    cache=getattr(service,'_verified_json_reads',None) or VerifiedJsonReads()
    return cache.read(_vault_path(service.vault,pointer['report_ref']),expected=pointer['report_sha256'],
        origin='fixed_history.checked_report')


def history_state(service,workflow_id):
    read=getattr(service,'status_fields',None)
    return read(workflow_id,('execution_recovery_history',)) if read else service.status(workflow_id)


def entries(adapter,binding,*,scopes=None,reverse=False,select=None,query=None,
            code='STALE_INPUT',message='recovery evidence changed'):
    workflow=history_state(adapter.workflow,binding['workflow_id'])
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


def _rows(entry,kind):
    if kind=='build_review':return entry['build_review_recovery']['reviews']
    if kind=='output_dispositions':return entry.get(kind,[])
    value=entry.get(kind,{})
    if kind=='output_seed':
        value.get('journal_name')  # Preserve the old malformed-seed failure.
        return [value]
    return [value] if value else []


def _journal_name(row,kind):
    return row.get('journal_name') if kind=='output_seed' else row['journal_name']


def _journal_index(report,kind):
    """Lists preserve duplicate rows and original report/entry/row ordering."""
    try:
        scope=report['request'].get('recovery_scope');found={}
        for entry in report['entries']:
            for row in _rows(entry,kind):
                found.setdefault((entry['node'],_journal_name(row,kind)),[]).append(row)
        return {'scope':scope,'rows':found}
    except (KeyError,TypeError,AttributeError):
        # Do not eagerly raise for a malformed later row that an original lazy
        # caller would never visit. Fall back to the original entry iteration.
        return {'fallback':report}


def journal_rows(adapter,binding,journal,kind,*,scopes=None,reverse=False,
                 code='STALE_INPUT',message='recovery evidence changed'):
    workflow=history_state(adapter.workflow,binding['workflow_id'])
    cache=getattr(adapter.workflow,'_verified_json_reads',None) or VerifiedJsonReads()
    allowed=tuple(scopes) if scopes is not None else None
    def project(index):
        if 'fallback' in index:
            report=index['fallback']
            if allowed is not None and report['request'].get('recovery_scope') not in allowed:return (False,[])
            return (True,[e for e in report['entries'] if e['node']==binding['node']])
        if allowed is not None and index['scope'] not in allowed:return (False,[])
        return (False,index['rows'].get((binding['node'],journal),[]))
    pointers=workflow.get('execution_recovery_history',[])
    for pointer in reversed(pointers) if reverse else pointers:
        path=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        try:
            fallback,rows=cache.read(path,expected=pointer['report_sha256'],
                index=lambda report:_journal_index(report,kind),index_key=('journal_rows',kind),
                project=project,projection_key=('journal_rows',allowed,binding['node'],journal,kind))
        except ContractError as exc:
            if exc.code=='SOURCE_CHANGED':raise ContractError(code,'$',message) from exc
            raise
        if fallback:
            for entry in rows:
                for row in _rows(entry,kind):
                    if _journal_name(row,kind)==journal:yield pointer,row
        else:
            for row in rows:yield pointer,row
