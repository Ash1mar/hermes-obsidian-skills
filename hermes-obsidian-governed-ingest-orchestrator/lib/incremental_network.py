"""Freeze and continue an ordinary round at its native model transport boundary."""

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import pinned, read_pin
from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import fingerprint
from short_evidence_pass import require


class RoundNetworkPaused(Exception):
    def __init__(self, scope, evidence):
        self.scope = scope
        self.evidence = evidence
        super().__init__('round drained for network recovery')


def continuation(control, round_id, phase, batch):
    intent = control.status().get('network_continuation')
    if not intent or intent['round_id'] != round_id:
        return None, None
    evidence = intent['boundary']['evidence']
    if evidence['phase'] != phase:
        return None, None
    for pin in evidence['stage_reports']:
        report = read_pin(control.vault, pin)
        if report['batch'] == batch:
            return intent, report
    return None, None


def pause(domain, run_id, operation, batch, history, circuit):
    from prepare_short_evidence_test import save

    scope = operation['incremental']
    round_id, phase = scope['scope_id'], scope['stage']
    frozen = domain.round_input(run_id, round_id)
    round_input = frozen['input_fingerprint']
    report = {'contract': 'incremental-network-pause/v1', 'run_id': run_id,
              'round_id': round_id, 'phase': phase, 'round_input_fingerprint': round_input,
              'batch': batch, 'rows': history, 'circuits': [circuit],
              'probe_request_id': next((key for key, row in history.items() if row['state'] == 'unsent'), None)}
    root = domain.root(run_id) / 'rounds' / round_id / 'network-recovery'
    path = root / (fingerprint(report) + '.json')
    save(path, report)
    return RoundNetworkPaused({'scope_kind': 'round', 'scope_id': round_id}, {
        'phase': phase, 'round_input_fingerprint': round_input,
        'stage_reports': [pinned(domain.vault, path.relative_to(domain.vault).as_posix())],
        'circuits': [circuit],
    })


def settled_results(domain, run_id, root, rows, history, settle_usage):
    results = []
    for row in rows:
        proof = history[row['id']].get('proof')
        if not proof:
            continue
        result = read_pin(domain.vault, proof['result'])
        settle_usage(domain, run_id, root, row, result)
        results.append((row, result, proof))
    return results


def execute(domain, run_id, root, operation, rows, settle_usage):
    import run_short_evidence_test as native
    from incremental_writing import PHASE as PAGE_FRAGMENT
    from model_transport import circuit_status, retire_circuit_probe
    from prepare_short_evidence_test import save

    scope = operation['incremental']
    by_id = {row['id']: row for row in rows}
    require(len(by_id) == len(rows), 'network recovery batch repeats a request')
    batch = fingerprint(list(by_id))
    finished = root / 'network-batches' / (batch + '.json')
    control = IncrementalControl(domain.vault, run_id)
    intent, report = continuation(control, scope['scope_id'], scope['stage'], batch)
    network_scope = native.network_scope(root, operation)
    capacity = (operation['page_capacity'] if rows[0]['phase'] in ('incremental-page-write', PAGE_FRAGMENT)
                else operation['pass_capacity'])

    def history():
        return native.network_history(root, operation, rows, domain.status(run_id)['reservations'])

    current = history()
    if finished.is_file():
        require(_load_json(finished) == {'requests': list(by_id)}
                and all(row['state'] != 'unsent' for row in current.values()),
                'finished network batch differs from the original native results')
        return settled_results(domain, run_id, root, rows, current, settle_usage)

    circuit = circuit_status(network_scope)
    require(not report or circuit is not None, 'saved network continuation lost its HTTP journal route')
    evidence_only = False
    try:
        if circuit and report:
            original, = report['circuits']
            selected = report['probe_request_id']
            require(circuit['route_id'] == original['route_id'], 'saved network continuation route changed')
            if circuit['state'] == 'open' and circuit['trip_id'] == original['trip_id']:
                if selected is None:
                    # There is no admissible request with which to probe. Let this exact
                    # authorized batch consume its saved results without sending anything.
                    require(all(row['state'] != 'unsent' for row in current.values()),
                            'network pause evidence omitted an unsent request')
                    evidence_only = True
                elif current[selected]['state'] == 'unsent':
                    native.stage(root, operation, [by_id[selected]], 1, network_probe={
                        'route_id': circuit['route_id'], 'trip_id': circuit['trip_id'],
                        'operation_id': intent['operation_id'], 'request_id': selected})
                    current = history()
                elif circuit.get('probe_scope') == network_scope and circuit.get('probe_operation_id') == intent['operation_id']:
                    require(circuit['probe_request_id'] == selected, 'in-flight network probe differs from its authorization')
                    circuit = retire_circuit_probe(network_scope, route_id=circuit['route_id'], trip_id=circuit['trip_id'],
                                                   operation_id=intent['operation_id'], request_id=selected)
                    raise native.NetworkStagePaused(root, circuit)
            elif circuit['state'] == 'closed' and selected and current[selected]['state'] == 'unsent':
                # Another continuation may already have reopened the common route.
                # This round still checks one of its own original requests first.
                native.stage(root, operation, [by_id[selected]], 1)
                current = history()
                if current[selected]['state'] != 'completed':
                    raise native.NetworkStagePaused(root, original)
        if not evidence_only:
            waiting = [row for row in rows if current[row['id']]['state'] == 'unsent']
            native.stage(root, operation, waiting, capacity)
    except native.NetworkStagePaused as error:
        current = history()
        settled_results(domain, run_id, root, rows, current, settle_usage)
        raise pause(domain, run_id, operation, batch, current, error.circuit) from None

    current = history()
    require(all(row['state'] != 'unsent' for row in current.values()), 'native stage left undispatched work without a network pause')
    results = settled_results(domain, run_id, root, rows, current, settle_usage)
    save(finished, {'requests': list(by_id)})
    return results
