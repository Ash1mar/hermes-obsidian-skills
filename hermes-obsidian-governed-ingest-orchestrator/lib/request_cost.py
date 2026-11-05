"""Offline full-request text measurement; never substitute estimates for usage.

An explicit local tokenizer may be supplied by the operator. No model-name guess,
download or hidden network access is allowed. Images and provider framing remain
unmeasured, so these counts are comparisons, not billable input-token claims.
"""
import json,os
from functools import lru_cache

@lru_cache(maxsize=8)
def tokenizer(name):
    if not name:return None
    try:
        import tiktoken
        return tiktoken.get_encoding(name)
    except (ImportError,ValueError,OSError):return None

def measure(wire,transport_schema,images=(),*,encoding=None):
    text=json.dumps({'messages':wire,'response_schema':transport_schema},ensure_ascii=False,separators=(',',':'))
    name=encoding if encoding is not None else os.environ.get('HERMES_INPUT_TOKEN_ENCODING','')
    encoder=tokenizer(name)
    return {'contract':'full-request-cost/v1','codepoints':len(text),'utf8_bytes':len(text.encode('utf-8')),
        'text_tokens':len(encoder.encode(text,disallowed_special=())) if encoder else None,
        'tokenizer':name if encoder else None,'token_status':'measured_text_only' if encoder else 'unavailable',
        'image_count':len(images),'billable_tokens':None,
        'scope':'complete rendered messages, decoding instruction and effective schema; provider framing/image tokens excluded'}

def rank(prepared):
    cost=prepared.get('request_cost',{})
    # Preserve the established bound/selection when no explicit tokenizer exists.
    return (cost['text_tokens'] if cost.get('text_tokens') is not None else prepared['input_size'],prepared['input_size'])

def detail_reason(phase,view,correction=None):
    if correction:return 'semantic_correction'
    if phase!='global-detail':return None
    if 'page_budget' in view:return 'page_input_budget'
    if view.get('coordination',{}).get('detail_reason'):return 'grouping_ambiguity'
    return 'explicit_or_legacy_detail'
