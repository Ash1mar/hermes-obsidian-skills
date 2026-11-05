"""Offline full-request text measurement; never substitute estimates for usage.

An explicit local tokenizer may be supplied by the operator. No model-name guess,
download or hidden network access is allowed. Images and provider framing remain
unmeasured, so these counts are comparisons, not billable input-token claims.
"""
import json,os,hashlib,tempfile,types
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=8)
def tokenizer(name):
    if not name:return None
    try:
        import tiktoken
        import tiktoken.registry as registry
        from tiktoken.load import load_tiktoken_bpe
        # These constructors accept BPE ranks through this helper. Clone their
        # globals locally: never monkeypatch another request's tokenizer loader.
        if name not in ('o200k_base','cl100k_base'):return None
        tiktoken.list_encoding_names()
        constructor=registry.ENCODING_CONSTRUCTORS[name]
        def offline_ranks(url,expected_hash=None):
            cache=Path(os.environ.get('TIKTOKEN_CACHE_DIR',os.environ.get('DATA_GYM_CACHE_DIR',str(Path(tempfile.gettempdir())/'data-gym-cache'))))
            path=cache/hashlib.sha1(url.encode()).hexdigest()
            if not expected_hash or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_hash:
                raise ValueError('offline tokenizer ranks unavailable')
            return load_tiktoken_bpe(str(path),expected_hash=expected_hash)
        local=types.FunctionType(constructor.__code__,{**constructor.__globals__,'load_tiktoken_bpe':offline_ranks})
        return tiktoken.Encoding(**local())
    except (ImportError,ValueError,OSError,KeyError):return None

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
    if 'scope_revision' in view:return 'explicit_scope_revision'
    if 'page_budget' in view:return 'page_input_budget'
    if view.get('coordination',{}).get('detail_reason'):return 'grouping_ambiguity'
    return 'explicit_or_legacy_detail'
