"""Check physical destinations while keeping native logical plans immutable."""
import posixpath,re
from urllib.parse import unquote,urlsplit
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


def check_links(pages,destinations):
    """Never silently break an authored reference when adding a storage root."""
    if all(p==d for p,d in destinations.items()):return
    aliases={p.removesuffix('.md'):d for p,d in destinations.items()}
    for page in pages:
        text=page.get('content','');original=page['path'];physical=destinations[original]
        # Rooted URLs and fragment-only references keep their Vault destination.
        refs=re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',text)
        refs += re.findall(r'(?m)^\s*\[[^\]]+\]:\s*(\S+)',text)
        refs += re.findall(r'''(?:href|src)\s*=\s*["']([^"']+)["']''',text)
        for link in refs:
            link=link.strip('<>');part=urlsplit(link)
            if part.scheme or part.netloc or not part.path or part.path.startswith('/'):continue
            target=unquote(part.path)
            old=posixpath.normpath(posixpath.join(posixpath.dirname(original),target))
            new=posixpath.normpath(posixpath.join(posixpath.dirname(physical),target))
            wanted=destinations.get(old,old)
            if old.startswith('../') or new!=wanted:
                raise ContractError('INVALID_OUTPUT_PATH','$',
                    'knowledge-root routing would change an authored relative reference; obtain a native correction, never rewrite prose or links')
        for token in re.findall(r'\[\[([^\]]+)\]\]',text):
            path=unquote(token.split('|')[0].split('#')[0]).removesuffix('.md')
            if '/' in path and path in aliases and aliases[path].removesuffix('.md')!=path:
                raise ContractError('INVALID_OUTPUT_PATH','$',
                    'path-qualified authored wiki reference needs its exact routed destination')
