"""Process-local fixed-call watchdog default; never an API retry allowance."""
import math,os

IDLE_ENV='HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS'

def configure():
    from orchestration import configuration
    default=configuration().get('native_stream_idle_seconds',120)
    if isinstance(default,bool) or not isinstance(default,(int,float)) or not math.isfinite(default) or not 30<=default<=600:
        raise ValueError('native stream idle default must be finite and within 30..600 seconds')
    explicit=IDLE_ENV in os.environ
    value=os.environ.setdefault(IDLE_ENV,str(default))
    try:seconds=float(value)
    except ValueError as exc:raise ValueError('invalid explicit native stream idle timeout') from exc
    if not math.isfinite(seconds):raise ValueError('native stream idle timeout must be finite')
    return {'contract':'fixed-native-stream-policy/v1','idle_seconds':seconds,
            'configured_default_seconds':default,'source':'environment' if explicit else 'fixed_worker_default',
            'ttfb_policy':'native_unchanged','total_budget_seconds':600,'retries_granted':0}
