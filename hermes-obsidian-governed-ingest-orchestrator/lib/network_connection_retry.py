"""Persistent connection-only backoff within one fixed semantic reservation.

Every ambiguous transport failure stays auditable. A returned response exits
this loop; validation/semantic corrections and other unknowns keep their bounds.
"""
import copy,time
from pathlib import Path
from hermes_source_units.source_units import _write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.runtime_telemetry import event,span

CONTRACT='native-connection-retry/v1'


def connection_error(error):
    from openai import APIConnectionError
    # APITimeoutError is a subclass, but has a separate unknown/timeout policy.
    if type(error) is not APIConnectionError:return False
    body=getattr(error,'body',None)
    return not (getattr(error,'response_id',None) or isinstance(body,dict) and
        (body.get('partial') or body.get('output') or body.get('choices')))


def retry(agent,error,request_sha256):
    context=getattr(agent,'fixed_network_context',None)
    if not context or not connection_error(error):return False
    from native_call_state import StopNativeCall
    def check(*,fresh=False):
        try:
            context['control_check']()
            if fresh:context['fresh_check']()
        except StopNativeCall:raise
        except Exception as exc:
            raise StopNativeCall({'contract':'native-call-outcome/v1','state':'request_rejected',
                'retryable':False,'source':'local_request_guard',
                'error':{'class':type(exc).__name__,'message':str(exc)}}) from exc
    refs=agent.fixed_network_retry_refs
    ordinal=len(refs)+1;delay=min(60,5*2**min(ordinal-1,4))
    binding=context['binding'];vault=Path(context['vault'])
    relative=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/network-retries/{agent.session_id}-{ordinal}.json"
    record={'contract':CONTRACT,'session_id':agent.session_id,'binding':copy.deepcopy(binding),
        'phase':context['phase'],'request_sha256':request_sha256,'wire_sha256':context['wire_sha256'],
        'network_failure_ordinal':ordinal,'state':'completion_unknown','error_class':'APIConnectionError',
        'recorded_at_ms':int(time.time()*1000),'next_backoff_seconds':delay,
        'semantic_ordinals_granted':0,'response_received':False,'retry_until':'connection_returns_or_control_stops',
        'previous_failure_ref':refs[-1]['ref'] if refs else None}
    path=vault/relative
    if path.exists():raise RuntimeError('network failure evidence identity collision')
    _write_atomic(path,_json_bytes(record));refs.append({'ref':relative,'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest()})
    event('network_connection_failure',vault=vault,binding=binding,phase=context['phase'],session_id=agent.session_id,
        network_failure_ordinal=ordinal,request_sha256=request_sha256,evidence_ref=relative)
    start=time.monotonic()
    with span('network_retry_wait',vault=vault,binding=binding,phase=context['phase'],session_id=agent.session_id,
              network_failure_ordinal=ordinal,backoff_seconds=delay):
        deadline=start+delay
        while True:
            check();remaining=deadline-time.monotonic()
            if remaining<=0:break
            time.sleep(min(2,remaining))
        check(fresh=True)
    agent.fixed_network_wait_ms+=int((time.monotonic()-start)*1000)
    return True
