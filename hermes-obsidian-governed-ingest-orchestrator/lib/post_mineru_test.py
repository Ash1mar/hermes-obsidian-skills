"""Hash-bound conversion import for independent tests, never semantic reuse.

The ordinary startup CLI cannot enable import. Only the separate trusted test
entry invokes import_bundles before dispatch. Native source workers still register
their new identity, finish ingest, build and validate their own UnitSets normally.
"""
import copy,hashlib,json,os,sys
from pathlib import Path,PurePosixPath
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_write_atomic,_json_bytes,_vault_path
from hermes_source_units.validation import fingerprint

CONTRACT='post-mineru-test/v1'

def fail(message):raise ContractError('TEST_BUNDLE_IMPORT_INVALID','$',message)
def sha(data):return hashlib.sha256(data).hexdigest()
def conversion(manifest):
    value=copy.deepcopy(manifest);value.pop('governance',None);return value

def inventory(bundle):
    if bundle.is_symlink() or not bundle.is_dir():fail('Bundle must be a real directory')
    result={}
    for p in sorted(bundle.rglob('*')):
        if p.is_symlink():fail('symlink in Bundle')
        if p.is_file():
            if not p.resolve().is_relative_to(bundle.resolve()):fail('Bundle path escapes root')
            result[p.relative_to(bundle).as_posix()]=sha(p.read_bytes())
    if 'manifest.json' not in result:fail('missing Bundle manifest')
    return result

def validate(bundle):
    scripts=Path(__file__).resolve().parents[2]/'hermes-obsidian-controlled-ingest/scripts'
    if str(scripts) not in sys.path:sys.path.insert(0,str(scripts))
    from validate_document_bundle import validate_bundle
    result=validate_bundle(bundle)
    if result['status']=='fail':fail('Bundle validator failed')
    return result

def _verify_row(vault,row):
    bundle=_vault_path(vault,row['bundle_ref']);actual=inventory(bundle)
    if set(actual)!=set(row['imported_files']):fail('imported Bundle membership changed')
    for name,h in row['imported_files'].items():
        if name!='manifest.json' and actual[name]!=h:fail('imported Bundle bytes changed: '+name)
    m=_load_json(bundle/'manifest.json')
    if fingerprint(conversion(m))!=row['conversion_sha256']:fail('imported conversion/QA changed')
    if m.get('governance') and m['governance'].get('vault_id')!=row['target_vault_id']:fail('foreign governance identity retained')
    if sha((_vault_path(vault,row['raw_ref'])).read_bytes())!=row['source_sha256']:fail('target original changed')
    return bundle

def import_bundles(vault,request):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):fail('trusted test operator required')
    if request.get('test_mode')!=CONTRACT:fail('test mode required')
    origin=Path(request['reuse_conversion']['vault']).resolve(strict=True)
    if origin==vault.resolve() or origin.is_relative_to(vault.resolve()) or vault.resolve().is_relative_to(origin):fail('independent Vault roots required')
    wid='ingest-'+request['operation_id'];record_ref=f'_system/reports/post-mineru-tests/{request["operation_id"]}.json'
    path=_vault_path(vault,record_ref);pin=fingerprint(request['reuse_conversion'])
    if path.exists():
        record=_load_json(path)
        if (record.get('contract')!=CONTRACT or record['workflow_id']!=wid or record['import_pin']!=pin or record['vault_id']!=request['vault_id']):fail('import receipt identity changed')
        for row in record['sources']:_verify_row(vault,row)
        return {'ref':record_ref,'sha256':sha(path.read_bytes()),'contract':CONTRACT}
    if (vault/f'_system/ledgers/ingest-workflows/{wid}.json').exists():fail('cannot insert test materials into an existing workflow')
    ow=request['reuse_conversion']['workflow_id']
    w=_load_json(_vault_path(origin,f'_system/ledgers/ingest-workflows/{ow}.json'))
    if w['state'] not in ('completed','partial'):fail('origin workflow must be terminal')
    from native_stop_inventory import workflow_processes
    if workflow_processes(ow,w.get('kanban',{}).get('board_id')):fail('origin still has consumers')
    rows=[];inputs={Path(p).name:sha(Path(p).read_bytes()) for p in request['sources']}
    supplied=request['reuse_conversion']['sources']
    if len(supplied)!=len(inputs) or {r['filename'] for r in supplied}!=set(inputs):fail('exact source coverage required')
    # Check every origin before writing any new candidate material.
    checked=[]
    for q in supplied:
        relative=PurePosixPath(q['bundle_ref'])
        if relative.parts[:3]!=('_system','reports','source-bundles') or '..' in relative.parts or relative.is_absolute():fail('origin must be a governed conversion Bundle')
        bundle=_vault_path(origin,q['bundle_ref'])
        if inventory(bundle)!=q['files']:fail('origin Bundle inventory/hash changed')
        m=_load_json(bundle/'manifest.json');source=m.get('source',{})
        outcome=next((r for r in w['source_outcomes'] if r['path']=='10_Raw/'+q['filename']),None)
        if (not outcome or outcome['status']!='ready' or q['bundle_ref']+'/manifest.json' not in outcome['artifact_refs'] or
                source.get('sha256')!=q['source_sha256'] or inputs[q['filename']]!=q['source_sha256'] or
                outcome['content_sha256']!=q['source_sha256']):fail('origin Bundle/source identity differs')
        checked.append((q,bundle,m,validate(bundle)))
    for q,source,original,quality in checked:
        relative=f'_system/reports/source-bundles/post-mineru-test-{q["source_sha256"]}'
        target=_vault_path(vault,relative);m=conversion(original)
        if target.exists() and not set(inventory(target))<=set(q['files']):fail('unexpected existing candidate files')
        m['source']['path']=str(_vault_path(vault,'10_Raw/'+q['filename']))
        for name,h in q['files'].items():
            rel=PurePosixPath(name)
            if rel.is_absolute() or '..' in rel.parts or any(x in rel.parts for x in ('semantic-calls','page-drafts','output-attempts')):fail('unsafe or non-conversion file')
            dest=_vault_path(vault,relative+'/'+name);payload=_json_bytes(m) if name=='manifest.json' else (source/name).read_bytes()
            if name!='manifest.json' and sha(payload)!=h:fail('origin changed during copy')
            if dest.exists() and dest.read_bytes()!=payload:fail('candidate import conflicts with existing file')
            if not dest.exists():_write_atomic(dest,payload)
        if inventory(source)!=q['files']:fail('origin changed after copy')
        valid=validate(target)
        row={'filename':q['filename'],'raw_ref':'10_Raw/'+q['filename'],'source_sha256':q['source_sha256'],
            'origin_vault':str(origin),'origin_workflow_id':ow,'origin_bundle_ref':q['bundle_ref'],
            'origin_files':q['files'],'origin_manifest':original,'bundle_ref':relative,'target_vault_id':request['vault_id'],
            'imported_files':inventory(target),'conversion_sha256':fingerprint(conversion(m)),
            'origin_validation':quality,'import_validation':valid,'semantic_results_imported':False,'native_binding_pending':True}
        _verify_row(vault,row);rows.append(row)
    record={'contract':CONTRACT,'workflow_id':wid,'vault_id':request['vault_id'],'import_pin':pin,
        'sources':rows,'mineru_calls':0,'semantic_results_imported':False}
    _write_atomic(path,_json_bytes(record))
    return {'ref':record_ref,'sha256':sha(path.read_bytes()),'contract':CONTRACT}

def imported_bundle(adapter,binding,source):
    w=adapter.workflow.status(binding['workflow_id']);ref=w['scope'].get('bootstrap_ref')
    if not ref:return None
    report=_load_json(_vault_path(adapter.workflow.vault,ref));pointer=report.get('test_bundle_import')
    if not pointer:return None
    if pointer.get('contract')!=CONTRACT:fail('unknown import contract')
    path=_vault_path(adapter.workflow.vault,pointer['ref']);raw=path.read_bytes()
    if sha(raw)!=pointer['sha256']:fail('import receipt changed')
    record=json.loads(raw)
    if record.get('contract')!=CONTRACT or record['workflow_id']!=binding['workflow_id'] or record['vault_id']!=_load_json(adapter.workflow.vault/'_system/vault.json')['vault']['id']:
        fail('foreign import receipt')
    rows=[r for r in record['sources'] if r['raw_ref']==source['path'] and r['source_sha256']==source['content_sha256']]
    if len(rows)!=1:fail('assigned source not exactly imported')
    return _verify_row(adapter.workflow.vault,rows[0]).relative_to(adapter.workflow.vault).as_posix()
