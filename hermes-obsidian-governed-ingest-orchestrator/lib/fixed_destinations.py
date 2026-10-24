"""Check physical destinations while keeping native logical plans immutable."""
import posixpath,re
from urllib.parse import unquote,urlsplit,urlunsplit,quote
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.knowledge_destinations import destination


def routes(value,pages):
    occupied={p['path'].casefold():fingerprint({k:p['identity'][k] for k in ('kind','identity_key')})
              for p in value['view'].get('existing_pages',[])}
    result={};used={}
    for page in pages:
        key=fingerprint({k:page['identity'][k] for k in ('kind','identity_key')})
        old=value['backend']['existing_pages'].get(key)
        if old and page['path']!=old['subject']['current_path']:
            raise ContractError('IDENTITY_CONFLICT','$','update must retain the exact registered destination')
        target=destination(page['path'],existing=bool(old));physical=target.casefold()
        if physical in used:
            raise ContractError('IDENTITY_CONFLICT','$','logical page paths collide after knowledge-root routing')
        if physical in occupied and occupied[physical]!=key:
            raise ContractError('IDENTITY_CONFLICT','$','routed destination belongs to another registered identity')
        used[physical]=page['path'];result[page['path']]=target
    return result


def _asset_paths(value,page):
    handles=set(page['candidate_refs'])
    readings={r['reading'] for c in value['view']['candidates'] if c['candidate'] in handles for r in c['source_evidence']}
    return {m['asset']['path'] for task in value['view'].get('original_evidence',{}).get('tasks',[])
        if task['task'] in readings for m in task['materials'] if m.get('asset')
        and m['asset'].get('permission')=='this registered whole asset only'
        and m['asset']['path'].startswith('_system/sources/artifacts/')}


def _reference_edits(pages,destinations,value=None):
    """Rebase only exact, already authorized original assets; never infer a link."""
    edits=[]
    if all(p==d for p,d in destinations.items()):return edits
    aliases={p.removesuffix('.md'):d for p,d in destinations.items()}
    for page in pages:
        text=page.get('content','');original=page['path'];physical=destinations[original]
        assets=_asset_paths(value,page) if value else set()
        # Rooted URLs and fragment-only references keep their Vault destination.
        patterns=(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',
            r'(?m)^\s*\[[^\]]+\]:\s*(\S+)',r'''(?:href|src)\s*=\s*["']([^"']+)["']''')
        refs={m.span(1):m.group(1) for pattern in patterns for m in re.finditer(pattern,text)}
        previous_end=-1
        for (start,end),raw in sorted(refs.items()):
            if start<previous_end:raise ContractError('INVALID_OUTPUT_PATH','$','ambiguous overlapping link syntax')
            previous_end=end
            link=raw.strip('<>');part=urlsplit(link)
            if part.scheme or part.netloc or not part.path or part.path.startswith('/'):continue
            target=unquote(part.path)
            old=posixpath.normpath(posixpath.join(posixpath.dirname(original),target))
            new=posixpath.normpath(posixpath.join(posixpath.dirname(physical),target))
            wanted=destinations.get(old,old)
            if old.startswith('../') or new!=wanted:
                if old not in assets or wanted!=old:
                    raise ContractError('INVALID_OUTPUT_PATH','$',
                        'knowledge-root routing would change an authored relative reference outside exact authorized originals')
                relative=posixpath.relpath(old,posixpath.dirname(physical) or '.')
                assert posixpath.normpath(posixpath.join(posixpath.dirname(physical),relative))==old
                replacement=urlunsplit(('','',quote(relative,safe='/@-._~'),part.query,part.fragment))
                if raw.startswith('<') and raw.endswith('>'):replacement='<'+replacement+'>'
                edits.append({'logical_path':original,'start':start,'end':end,'original_href':raw,
                    'submitted_href':replacement,'canonical_asset_path':old})
        for token in re.findall(r'\[\[([^\]]+)\]\]',text):
            path=unquote(token.split('|')[0].split('#')[0]).removesuffix('.md')
            if '/' in path and path in aliases and aliases[path].removesuffix('.md')!=path:
                raise ContractError('INVALID_OUTPUT_PATH','$',
                    'path-qualified authored wiki reference needs its exact routed destination')
    return edits


def check_links(pages,destinations):
    _reference_edits(pages,destinations)


def compile_content(value,page,destinations):
    """Retain raw native prose; typed submission preserves each asset URI target."""
    edits=_reference_edits([page],destinations,value)
    content=page['content']
    for edit in reversed(edits):
        assert content[edit['start']:edit['end']]==edit['original_href']
        content=content[:edit['start']]+edit['submitted_href']+content[edit['end']:]
    return content,edits
