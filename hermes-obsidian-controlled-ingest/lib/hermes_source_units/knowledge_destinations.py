"""Mechanical knowledge-root routing, independent of semantic identity or prose."""
from pathlib import PurePosixPath
import re
from .validation import ContractError

ROOTS=('20_Notes','30_Cards','40_Concepts','50_Projects')
DEFAULT_ROOT='30_Cards'


def governed(path):
    return path.split('/')[0] in ROOTS and PurePosixPath(path).suffix.lower()=='.md'


def destination(path,*,existing=False):
    """Retain the model's relative topic path beneath a required storage root.

    Existing destinations are exact offers and may never be moved implicitly.
    This function does not touch files, identities, candidates or authored text.
    """
    if (not isinstance(path,str) or not path or '\\' in path or '\x00' in path
            or path.startswith('/') or re.match(r'^[A-Za-z]:',path)
            or any(p in ('','.','..') for p in path.split('/'))
            or PurePosixPath(path).suffix.lower()!='.md'):
        raise ContractError('INVALID_OUTPUT_PATH','$','knowledge destination must be safe relative Markdown')
    if existing:
        if not governed(path):
            raise ContractError('INVALID_OUTPUT_PATH','$','existing destination needs explicit migration, not implicit routing')
        return path
    head,separator,tail=path.partition('/')
    root=next((r for r in ROOTS if r.casefold()==head.casefold()),None)
    return root+separator+tail if root else DEFAULT_ROOT+'/'+path
