"""Deterministic reader navigation projected from the current qualified release."""
from contextlib import nullcontext
from html import escape
import json
from pathlib import PurePosixPath
import posixpath
import re
from urllib.parse import quote

from hermes_source_units.short_build import evidence
from hermes_source_units.source_units import _exclusive_lock, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError
from hermes_source_units.vault_finalize import RELEASE_ROOT
from short_cards import heading, label, markdown_label
from short_evidence_pass import require

CONTRACT = 'short-knowledge-navigation/v1'
INDEX = '90_Dataview/短路径知识导航.md'
MARKER = re.compile(rb'<!-- hermes-derived-index/v1 sha256:([0-9a-f]{64}) -->')
TYPES = {'card': '卡片', 'concept': '概念', 'entity': '实体', 'topic': '专题', 'project': '项目'}
OPTIONAL_ERRORS = (ContractError, OSError, ValueError, KeyError, TypeError)


def managed_index(vault, ref, text, *, apply):
    """Only replace an intact generated file; a human edit breaks its checksum."""
    path = _vault_path(vault, ref)
    if path != vault / ref or path.is_symlink() or (path.exists() and not path.is_file()):
        return {'path': ref, 'state': 'occupied', 'changed': False}
    body = text.encode('utf-8')
    old = path.read_bytes() if path.exists() else None
    if old is not None:
        first, _, prior = old.partition(b'\n')
        marker = MARKER.fullmatch(first)
        if not marker:
            return {'path': ref, 'state': 'occupied', 'changed': False}
        if marker[1].decode('ascii') != _sha(prior):
            return {'path': ref, 'state': 'manual_edit', 'changed': False}
        if prior == body:
            return {'path': ref, 'state': 'ready', 'changed': False}
    if apply:
        latest = path.read_bytes() if path.is_file() else None
        if latest != old or path.is_symlink() or (path.exists() and not path.is_file()):
            return {'path': ref, 'state': 'manual_edit', 'changed': False}
        _write_atomic(path, f'<!-- hermes-derived-index/v1 sha256:{_sha(body)} -->\n'.encode() + body)
    return {'path': ref, 'state': 'ready' if apply else 'planned', 'changed': apply}


def source_labels(service):
    governance = service.source.vault_manifest['governance']
    data = _load_json(_vault_path(service.vault, governance['repository']['registry_path']))
    return {row['resource_id']: ' · '.join(label(part) for part in (row.get('title'), row.get('business_version'))
                                            if isinstance(part, str) and part.strip())
            for row in data['records']}


def project(service, release):
    """Only use registered, eligible page identities. Optional metadata never controls eligibility."""
    gaps, pages, short_ids, builds = [], {}, {}, {}
    eligible = {row['id'] for row in release['index_eligibility'] if row['kind'] == 'knowledge_page' and row['eligible']}
    registry = {row['page_id']: row for row in service.build._registry()['subjects'] if row['status'] == 'active'}
    try:
        sources = source_labels(service)
    except OPTIONAL_ERRORS:
        sources = {}
        gaps.append({'reason': 'source_titles_unavailable'})
    for row in release['subjects']:
        pid, path = row['page_id'], row['current_path']
        if pid not in eligible:
            continue
        subject = registry.get(pid)
        try:
            target = _vault_path(service.vault, path)
            valid = (subject and subject['current_path'] == path
                     and subject['current_revision_id'] == row['current_revision_id']
                     and target == service.vault / path and target.is_file())
        except OPTIONAL_ERRORS:
            valid = False
        if not valid:
            gaps.append({'page_id': pid, 'reason': 'registered_target_unavailable'})
            continue
        identity = subject['identity_key']
        typed = subject.get('page_type')
        metadata = {}
        if identity.startswith('short-evidence:'):
            short_ids[identity.removeprefix('short-evidence:')] = pid
            run_id = subject['created_by_run']
            if run_id not in builds:
                try:
                    run = _load_json(service.build._run_path(run_id))
                    proof = evidence(service.build, run)
                    builds[run_id] = {(rev['page_id'], rev['revision_id']): spec for rev, spec in
                                      zip(run['page_revisions'], proof['scope']['pages'], strict=True)}
                except OPTIONAL_ERRORS:
                    builds[run_id] = {}
            metadata = builds[run_id].get((pid, row['current_revision_id']), {})
            if typed and not metadata:
                gaps.append({'page_id': pid, 'reason': 'optional_build_metadata_unavailable'})
        parts = PurePosixPath(path).parts
        theme = metadata.get('navigation_theme') or (
            parts[1] if len(parts) > 2 and parts[0] == '30_Cards' else None)
        pages[pid] = {'name': subject['canonical_name'], 'path': path, 'type': typed,
                      'theme': theme, 'project': (metadata.get('project_basis') or {}).get('name'),
                      'entries': [entry['short_id'] for entry in
                                  metadata.get('synthesis_basis', {}).get('entries', ())],
                      'resources': {ref['unit_ref']['resource_id'] for ref in row['active_support_refs']}}
    relations = {}
    for pid, page in pages.items():
        members = {short_ids[key] for key in page['entries'] if key in short_ids and short_ids[key] != pid}
        if len(members) < len(set(page['entries'])):
            gaps.append({'page_id': pid, 'reason': 'optional_entry_not_in_qualified_release'})
        if members:
            relations[pid] = members
    project_members = {}
    if any(page['type'] == 'project' for page in pages.values()):
        try:
            ref = f"{RELEASE_ROOT}/{release['release_id']}/navigation.json"
            data = _vault_path(service.vault, ref).read_bytes()
            require(_sha(data) == release['navigation_sha256'], 'published link projection changed')
            project_members = {row['page_id']: set(row['outgoing_page_ids']) & pages.keys()
                               for row in json.loads(data)['entries']
                               if row['page_id'] in pages and pages[row['page_id']]['type'] == 'project'}
        except OPTIONAL_ERRORS:
            gaps.append({'reason': 'published_project_links_unavailable'})
    return pages, sources, relations, project_members, sorted(
        gaps, key=lambda row: (row.get('page_id', ''), row['reason']))


def render(pages, sources, relations, project_members):
    def page_key(pid):
        row = pages[pid]
        return (label(row['name']).casefold(), row['path'])

    def link(pid):
        row = pages[pid]
        relative = posixpath.relpath(row['path'], str(PurePosixPath(INDEX).parent))
        return f"[{markdown_label(escape(label(row['name'])))}]({quote(relative, safe='/')})"

    def group(title, groups):
        if groups:
            lines.extend(['', '## ' + title])
        for _, (name, members) in sorted(groups.items(), key=lambda item: (item[1][0].casefold(), item[0])):
            lines.extend(['', '### ' + escape(heading(name)),
                          *(f'- {link(pid)}' for pid in sorted(members, key=page_key))])

    lines = ['# 知识导航', '', '此索引只列出当前发布中合格的页面。']
    by_type, by_theme, by_source = {}, {}, {}
    for pid, row in pages.items():
        page_type = row['type'] or ''
        theme = row['theme'] or '未登记主题'
        by_type.setdefault(page_type, (TYPES.get(page_type, '未标注类型'), set()))[1].add(pid)
        by_theme.setdefault(theme, (theme, set()))[1].add(pid)
        for resource in row['resources']:
            by_source.setdefault(resource, (sources.get(resource) or '未标注标题的来源', set()))[1].add(pid)
    group('按类型', by_type)
    group('按主题', by_theme)
    group('按来源', by_source)
    projects = sorted((pid for pid, row in pages.items() if row['type'] == 'project'), key=page_key)
    if projects:
        lines.extend(['', '## 按项目'])
        for pid in projects:
            name = pages[pid]['project'] or pages[pid]['name']
            lines.append(f'- {escape(label(name))}：{link(pid)}')
            lines.extend(f'  - {link(member)}' for member in sorted(project_members.get(pid, ()), key=page_key))
    if relations:
        lines.extend(['', '## 专题与独立条目'])
        for topic in sorted(relations, key=page_key):
            lines.append(f'- {link(topic)}')
            lines.extend(f'  - {link(pid)}' for pid in sorted(relations[topic], key=page_key))
    return '\n'.join([*lines, ''])


def refresh(service, release, *, apply=False):
    lock = _vault_path(service.vault, f'{RELEASE_ROOT}/.finalize.lock')
    with (_exclusive_lock(lock) if apply else nullcontext()):
        release_id = release['release_id']
        require(service.status()['current_release_id'] == release_id, 'release changed before navigation')
        require(_load_json(service._manifest_path(release_id)) == release, 'navigation needs the exact release manifest')
        pages, sources, relations, project_members, gaps = project(service, release)
        text = render(pages, sources, relations, project_members)
        require(service.status()['current_release_id'] == release_id, 'release changed during navigation')
        return {**managed_index(service.vault, INDEX, text, apply=apply), 'pages': len(pages), 'gaps': gaps,
                'release_id': release_id}
