#!/usr/bin/env python3
"""Manage explicit incremental runs without changing historical workflow contracts."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units.incremental_control import IncrementalControl
from incremental_native import service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    start = sub.add_parser('start')
    start.add_argument('--request', type=Path, required=True)
    start.add_argument('--record', type=Path, required=True)
    start.add_argument('--bootstrap-skill', type=Path, required=True)
    for action in ('status', 'watch'):
        sub.add_parser(action).add_argument('--run-id', required=True)
    for action in ('reconcile', 'preview-reuse', 'control', 'recover-coordination', 'recover-delivery', 'retry-source'):
        sub.add_parser(action).add_argument('--request', type=Path, required=True)
    for action in ('recover-writing', 'recover-closure', 'recover-map'):
        recovery = sub.add_parser(action)
        recovery.add_argument('--request', type=Path, required=True)
        recovery.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    try:
        if args.action in ('start', 'reconcile', 'control', 'recover-coordination', 'recover-delivery', 'retry-source', 'recover-writing', 'recover-closure', 'recover-map') and (os.environ.get('HERMES_KANBAN_TASK') or
                                                                os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')):
            raise ValueError('incremental operator actions require a trusted operator')
        if args.action == 'start':
            from incremental_runtime import start as begin
            request = json.loads(args.request.read_text(encoding='utf-8-sig'))
            if Path(request['vault_path']).resolve() != args.vault.resolve():
                raise ValueError('explicit Vault differs from the start request')
            result = begin(request, args.record, args.bootstrap_skill)
        elif args.action in ('status', 'watch'):
            if args.action == 'watch':
                from incremental_ingest_controller import watch
                result = watch(args.vault, args.run_id)
            else:
                domain = service(args.vault, args.run_id)
                result = {'run': domain.status(args.run_id), 'control': IncrementalControl(domain.vault, args.run_id).status()}
        else:
            request = json.loads(args.request.read_text(encoding='utf-8-sig'))
            domain = service(args.vault, request['run_id'])
            if args.action == 'preview-reuse':
                result = domain.preview_reuse(request)
            elif args.action == 'retry-source':
                if request.get('retry_kind') == 'preparation-unfinished':
                    from incremental_preparation_retry import retry
                    result = retry(domain, request)
                else:
                    from incremental_source_retry import retry
                    result = retry(domain.vault, request)
            elif args.action == 'recover-coordination':
                from incremental_ingest_controller import recover_coordination
                result = recover_coordination(domain, request)
            elif args.action == 'recover-writing':
                if 'revise_fragments' in request:
                    from incremental_writing_amendment import recover
                else:
                    from incremental_writing_resume import recover
                result = recover(domain, request, preview=args.preview)
            elif args.action == 'recover-closure':
                from incremental_closure import recover
                result = recover(domain, request, preview=args.preview)
            elif args.action == 'recover-map':
                from incremental_map_operator import recover
                result = recover(domain.vault, request, preview=args.preview)
            elif args.action == 'control':
                from incremental_map_operator import prepare_child_continuation

                if {'writing_continuation', 'writing_amendment', 'partial_closure'} & request.keys():
                    raise ValueError('bound recovery requires its specific operator entry point')
                control = IncrementalControl(domain.vault, request['run_id'])
                run = domain.status(request['run_id'])
                request = prepare_child_continuation(domain, run, control, request)
                result = control.control(request)
                if result['state'] == 'active':
                    from incremental_ingest_controller import resume_run
                    resume_run(domain, request['run_id'])
                elif result['state'] == 'cancelled' and not run.get('map_supplement'):
                    from native_ingest import control_workflow
                    from hermes_source_units.validation import fingerprint
                    source = domain.vault / '_system/ledgers/ingest-workflows' / (run['source_workflow_id'] + '.json')
                    if source.is_file():
                        control_workflow(domain.vault, run['source_workflow_id'], 'cancel',
                                         'incremental-cancel-' + fingerprint([run['run_id'], request['operation_id']])[:64])
            elif args.action == 'recover-delivery':
                from incremental_ingest_controller import (delivery_checkpoint, delivery_recovery_stop,
                                                           recover_delivery, resume_run)
                from hermes_source_units.incremental_sources import safe_id
                from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _write_atomic

                required = {'run_id', 'round_id', 'operation_id', 'actor', 'expected_revision', 'disposition'}
                if (not required <= set(request)
                        or not set(request) - required <= {'stop_after', 'isolation', 'control_revision'}):
                    raise ValueError('delivery recovery requires one exact operator intent')
                safe_id(request['operation_id'])
                safe_id(request['round_id'])
                scoped = 'control_revision' in request
                follow = scoped and request['disposition'] == 'continue'
                with _exclusive_lock(domain.root(request['run_id']) / '.controller.lock', timeout=0):
                    run = domain.status(request['run_id'])
                    intent = domain.root(run['run_id']) / 'rounds' / request['round_id'] / 'recovery-requests' / (request['operation_id'] + '.json')
                    if request['actor'] != run['actor']:
                        raise ValueError('delivery recovery actor changed')
                    control = IncrementalControl(domain.vault, run['run_id'])
                    if intent.is_file():
                        if _load_json(intent) != request:
                            raise ValueError('delivery recovery operation identifies different inputs')
                    else:
                        if type(request['expected_revision']) is not int or request['expected_revision'] != run['revision']:
                            raise ValueError('delivery recovery run revision changed')
                        if scoped:
                            head = control.status()
                            if (run['state'] != 'active' or run['active_round_id'] != request['round_id']
                                    or head['state'] != 'active' or head['revision'] != request['control_revision']):
                                raise ValueError('delivery node lost its active round or control revision')
                        _write_atomic(intent, _json_bytes(request))
                    control.reconcile_executors()
                    stop_after = request.get('stop_after')
                    if scoped:
                        admission = intent.parent.parent / 'recovery-admissions' / intent.name
                        if admission.is_file():
                            stop_after = _load_json(admission)['stop_after']
                        else:
                            prefix = intent.parent.parent / 'delivery.json'
                            prior = _load_json(prefix)
                            boundary = delivery_recovery_stop(control.status(), request['round_id'], prior)
                            if boundary == prior['state'] or boundary == 'build_committed' or stop_after is None:
                                stop_after = boundary
                            _write_atomic(admission, _json_bytes({'stop_after': stop_after}))
                    result = recover_delivery(domain, run['run_id'], request['round_id'], request['operation_id'],
                                              disposition=request['disposition'], stop_after=stop_after,
                                              isolation=request.get('isolation'), settle_terminal=not follow)
                    if scoped:
                        delivery_checkpoint(control, request['round_id'], result)
                if follow:
                    result = {**result, 'controller': resume_run(domain, run['run_id'])}
            else:
                from incremental_ingest_controller import resume_run
                run = domain.status(request['run_id'])
                if request['actor'] != run['actor'] or request['expected_revision'] != run['revision']:
                    raise ValueError('reconcile actor or run revision changed')
                IncrementalControl(domain.vault, run['run_id']).check_admission({
                    'scope_kind': 'run', 'scope_id': run['run_id'], 'stage': 'prepare',
                    'input_fingerprint': run['start_fingerprint']})
                result = {'run': run, 'controller': resume_run(domain, run['run_id'])}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
