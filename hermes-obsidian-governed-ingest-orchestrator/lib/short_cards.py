"""Versioned short-card placement, rendering and release navigation."""
from collections import defaultdict
import json
from pathlib import PurePosixPath
import re
import unicodedata
from urllib.parse import quote

from hermes_source_units.short_build import PAGE_DIRECTORIES, PAGE_TYPES, TYPED_LAYOUT, output_path
from hermes_source_units.source_units import _exclusive_lock, _vault_path, _write_atomic
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from short_evidence_pass import require
from short_knowledge_plan import CONTRACT as KNOWLEDGE_PLAN

CONTRACT = 'short-card-layout/v1'
INDEX = '00_目录.md'
MANAGED = '<!-- hermes-short-card-navigation/v1 -->'
INVALID = re.compile(r'[<>:"/\\|?*\[\]#^\x00-\x1f]')
RESERVED = re.compile(r'(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?')


def label(value):
    return ' '.join(unicodedata.normalize('NFC', value).split())


def segment(value, fallback):
    part = INVALID.sub('－', label(value)).strip(' .') or fallback
    if RESERVED.fullmatch(part):
        part = '主题－' + part
    return part.encode('utf-8')[:136].decode('utf-8', errors='ignore').rstrip(' .')


def snapshot(vault):
    """Pin a view of existing identities and occupied paths for deterministic replay."""
    from short_navigation import CONTRACT as NAVIGATION_CONTRACT

    service = FileVaultFinalizeService(vault)
    registry = service.build._registry()['subjects']
    root = _vault_path(vault, '30_Cards')
    require(root == vault / '30_Cards', 'short-card root must be inside the registered Vault path')
    paths = {p.relative_to(vault).as_posix() for p in root.rglob('*')}
    for root_name, folder in (prefix for prefix in PAGE_DIRECTORIES.values() if len(prefix) == 2):
        directory = vault / root_name / folder
        if directory.is_dir() and not directory.is_symlink() and not directory.parent.is_symlink():
            paths.update(p.relative_to(vault).as_posix() for p in directory.iterdir())
    existing = {r['identity_key'].removeprefix('short-evidence:'): r['current_path']
                for r in registry if r['identity_key'].startswith('short-evidence:')}
    registered_pages = {r['page_id']: {
        'name': r['canonical_name'], 'path': r['current_path'], 'page_id': r['page_id'],
        'revision_id': r['current_revision_id'], 'page_type': r.get('page_type'),
        'short_id': (r['identity_key'].removeprefix('short-evidence:')
                     if r['identity_key'].startswith('short-evidence:') else None)}
        for r in registry if r['status'] == 'active'}
    themes = sorted({p.parts[1] for r in registry if r['status'] == 'active'
                     and (p := PurePosixPath(r['current_path'])).parts[0] == '30_Cards' and len(p.parts) >= 3})
    occupied_dirs = sorted({p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()}) if root.is_dir() else []
    return {'contract': TYPED_LAYOUT, 'local_fragment_scope': True, 'content_outcome_recovery': True,
            'knowledge_plan': KNOWLEDGE_PLAN, 'registered_pages': registered_pages,
            'navigation_contract': NAVIGATION_CONTRACT,
            'themes': themes, 'directories': occupied_dirs,
            'occupied': sorted(paths | {r['current_path'] for r in registry}), 'existing': existing}


def place(layout, specs):
    require(layout['contract'] in (CONTRACT, TYPED_LAYOUT), 'unknown short page layout')
    typed = layout['contract'] == TYPED_LAYOUT
    occupied = {p.casefold() for p in layout['occupied']}
    directories = {p.casefold(): p for p in (*layout['directories'], *layout['themes'])}
    placed = {}
    for page in sorted(specs, key=lambda row: row['id']):
        pid, name = page['id'], label(page['name'])
        require(name, 'short card requires a readable title')
        if pid in layout['existing']:
            placed[pid] = {**page, 'registered_path': layout['existing'][pid]}
            continue
        page_type = page.get('page_type')
        require((page_type in PAGE_DIRECTORIES) if typed else page_type is None, 'page type differs from its layout')
        parent = '/'.join(PAGE_DIRECTORIES[page_type]) if typed else '30_Cards'
        if not typed or page_type == 'card':
            theme = segment(page.get('theme_directory') or name, '知识主题')
            parent += '/' + directories.setdefault(theme.casefold(), theme)
        stem = segment(name, '知识卡片')
        path = f'{parent}/{stem}.md'
        if path.casefold() in occupied or stem.casefold() == INDEX[:-3].casefold():
            path = f'{parent}/{stem} · {pid[:8]}.md'
        require(path.casefold() not in occupied, 'short card destination is already occupied')
        occupied.add(path.casefold())
        placed[pid] = {**page, 'name': name, 'card_path': output_path(pid, path, page_type=page_type)}
    return [placed[page['id']] for page in specs]


def heading(value):
    return re.sub(r'^[#\s]+|[#\s]+$', '', label(value)).strip() or '内容'


def markdown_label(value):
    return value.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')


def render(page, sections):
    """Render consistent heading levels while preserving fenced source examples."""
    used = {'目录'.casefold(), heading(page['name']).casefold()}

    def unique(value):
        base = heading(value)
        name = base
        number = 2
        while name.casefold() in used:
            name = f'{base}（{number}）'
            number += 1
        used.add(name.casefold())
        return name

    toc, body = [], []
    for section, fragments in sections.items():
        name = unique(section[1])
        toc.append(f'- [{markdown_label(name)}](#{quote(name, safe="")})')
        body.append('## ' + name)
        for row in fragments:
            fragment = unique(row['heading'])
            toc.append(f'  - [{markdown_label(fragment)}](#{quote(fragment, safe="")})')
            body.append('### ' + fragment)
            text, subheadings = nested_markdown(row['content'], unique)
            body.append(text)
            toc.extend(f'{"  " * (level - 2)}- [{markdown_label(title)}](#{quote(title, safe="")})'
                       for level, title in subheadings)
    kind = PAGE_TYPES[page['page_type']] if 'page_type' in page else 'knowledge-card'
    metadata = '---\ntype: ' + kind + '\ntitle: ' + json.dumps(label(page['name']), ensure_ascii=False)
    if 'page_type' in page:
        metadata += '\npage_type: ' + page['page_type']
        if basis := page.get('project_basis'):
            metadata += '\nproject: ' + json.dumps(basis['name'], ensure_ascii=False)
    if 'knowledge_plan' in page:
        metadata += '\nscope: ' + json.dumps(label(page['scope']), ensure_ascii=False)
    metadata += '\n---'
    return '\n\n'.join([metadata, '# ' + heading(page['name']), '## 目录', '\n'.join(toc), *body]) + '\n'


def nested_markdown(content, unique):
    result, headings, fence, level = [], [], None, 3
    for line in content.splitlines():
        if match := re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line):
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not match[2].strip():
                fence = None
        elif fence is None:
            if match := re.match(r'^ {0,3}(#{1,6})[ \t]+(.+)$', line):
                level = min(6, 3 + len(match[1]), level + 1)
                title = unique(match[2])
                headings.append((level, title))
                line = '#' * level + ' ' + title
            elif (result and result[-1] and not re.match(r'^\s|[-*+#>]|\d+[.)]\s', result[-1])
                  and re.fullmatch(r' {0,3}(?:={2,}|-{2,})\s*', line)):
                title = unique(result[-1])
                headings.append((4, title))
                result[-1] = '#### ' + title
                level = 4
                continue
        result.append(line)
    require(fence is None, 'short-card fragment has an unclosed code fence')
    return '\n'.join(result), headings


def verify_fragment(sections):
    for section in sections:
        nested_markdown(section['content'], heading)


def write_navigation(service, release, directories, *, typed=False, guarded=False):
    """Refresh only managed indexes, from the exact eligible release under its lock."""
    if guarded:
        from short_navigation import MARKER, OPTIONAL_ERRORS, managed_index

    def is_index(path):
        if (guarded and path.resolve() != path) or path.is_symlink() or not path.is_file():
            return False
        data = path.read_bytes()
        return data.startswith((MANAGED + '\n').encode()) or (guarded and MARKER.fullmatch(data.partition(b'\n')[0]))

    paths = []
    lock = _vault_path(service.vault, '_system/knowledge-releases/.finalize.lock')
    with _exclusive_lock(lock):
        require(service.status()['current_release_id'] == release['release_id'], 'release changed before navigation')
        root = service.vault / '30_Cards' if guarded else _vault_path(service.vault, '30_Cards')
        card_indexes = () if guarded and root.is_symlink() else root.glob(f'*/{INDEX}')
        directories = set(directories) | {p.parent.relative_to(service.vault).as_posix()
                                         for p in card_indexes if is_index(p)}
        if typed:
            for prefix in PAGE_DIRECTORIES.values():
                if len(prefix) == 2:
                    directory = '/'.join(prefix)
                    index = service.vault / directory / INDEX if guarded else _vault_path(service.vault, f'{directory}/{INDEX}')
                    if index == service.vault / directory / INDEX and is_index(index):
                        directories.add(directory)
        eligible = {r['id'] for r in release['index_eligibility'] if r['kind'] == 'knowledge_page' and r['eligible']}
        names = {row['page_id']: row['canonical_name'] for row in service.build._registry()['subjects']}
        pages = defaultdict(list)
        for row in release['subjects']:
            path = PurePosixPath(row['current_path'])
            if row['page_id'] in eligible and str(path.parent) in directories:
                pages[str(path.parent)].append((names[row['page_id']], path.name))
        for directory in sorted(directories):
            ref = f'{directory}/{INDEX}'
            entries = sorted(pages[directory])
            body = '\n'.join(['# ' + heading(PurePosixPath(directory).name), '',
                              *(f'- [{markdown_label(name)}]({quote(filename, safe="")})' for name, filename in entries), ''])
            if guarded:
                try:
                    if entries or (service.vault / ref).exists():
                        paths.append({**managed_index(service.vault, ref, body, apply=True), 'pages': len(entries)})
                except OPTIONAL_ERRORS as error:
                    paths.append({'path': ref, 'state': 'deferred', 'reason': str(error)})
                continue
            path = _vault_path(service.vault, ref)
            if path != service.vault / ref or (path.exists() and (
                    not path.is_file() or not path.read_bytes().startswith((MANAGED + '\n').encode()))):
                paths.append({'path': ref, 'state': 'occupied'})
                continue
            if entries or path.exists():
                data = (MANAGED + '\n\n' + body).encode('utf-8')
                if not path.exists() or path.read_bytes() != data:
                    _write_atomic(path, data)
                paths.append({'path': ref, 'state': 'ready', 'pages': len(entries)})
    return paths
