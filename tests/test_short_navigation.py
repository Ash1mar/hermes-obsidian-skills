"""Verify derived navigation is deterministic and never a publication dependency."""
import json
import sys
from pathlib import Path
from urllib.parse import unquote

from test_p3_knowledge_build import publish_sources, vault
from hermes_source_units import FileVaultFinalizeService
from hermes_source_units import short_build
from hermes_source_units.build_acceptance import assess, record
from hermes_source_units.page_governance import POLICY
from hermes_source_units.source_units import _json_bytes, _sha
from hermes_source_units.validation import fingerprint

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/lib'))
import short_formal_build
from short_navigation import CONTRACT, INDEX, refresh


def test_optional_index_links_and_manual_edits_do_not_block_release(vault, tmp_path, monkeypatch):
    ref, = publish_sources(vault, ['# Evidence\n\nAn approved revision is required.\n'])
    service = FileVaultFinalizeService(vault)
    directory = tmp_path / 'program'
    directory.mkdir()
    op = directory / 'operation.json'
    op.write_bytes(_json_bytes({'vault': str(vault), 'origin': {'workflow_id': 'fixture'},
                                'card_layout': {'contract': short_build.TYPED_LAYOUT}}))
    topic, entry, project, held = [fingerprint(name) for name in ('topic', 'entry', 'project', 'held')]
    grant = {'actor': 'agent', 'pages': sorted([topic, entry, project]), 'publish': True, 'policy': POLICY,
             'base_release': None}
    job = directory / 'job.json'
    job.write_bytes(_json_bytes({'request': {'phase': 'short-formal-build', 'view': {'authorization': grant}}}))
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {'task_id': 'fixture', 'run_id': 1})

    def candidate(key, kind, name, path, body, **extra):
        return {'short_id': key, 'name': name, 'page_type': kind, 'card_path': path,
                'navigation_theme': '设计交付', 'authored_sha256': _sha(body), 'support_refs': [ref],
                'citation_bindings': {'S1-B1': ref}, 'native_fragments': [{'receipt_sha256': fingerprint(key)}],
                'qa_issues': [], 'qa_status': 'usable', 'publication_authorized': True, **extra}

    paths = {topic: '50_Projects/专题/交付专题.md', entry: '30_Cards/设计交付/版本核对.md',
             project: '50_Projects/项目/工程交付.md'}
    bodies = {pid: b'# Evidence\n\nRequired. [S1-B1]\n' for pid in paths}
    bodies[project] += ('\n[[' + paths[entry].removesuffix('.md') + ']]\n').encode('utf-8')
    scope = {
        'actor': 'agent', 'origin': {'workflow_id': 'fixture'}, 'operation_sha256': _sha(op.read_bytes()),
        'coverage_complete': False, 'authorization': grant, 'navigation_contract': CONTRACT, 'material_coverage': {},
        'pages': [
            candidate(topic, 'topic', '交付专题', paths[topic], bodies[topic],
                      synthesis_basis={'entries': [{'short_id': entry}, {'short_id': held}]}),
            candidate(entry, 'card', '版本核对', paths[entry], bodies[entry]),
            candidate(project, 'project', '工程交付', paths[project], bodies[project],
                      project_basis={'name': '北岸工程', 'refs': ['S1-B1']}),
        ],
        'excluded': [{'short_id': held, 'name': '未交付条目', 'page_type': 'card',
                      'card_path': '30_Cards/设计交付/未交付条目.md',
                      'disposition': {'category': 'unresolved', 'reason': 'missing required condition',
                                      'reopen_condition': 'verify the exact condition'}}],
    }
    run = short_build.admit(service.build, scope, bodies, directory, job)
    reviews = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'], 'parent_authored_sha256': None,
                'actor': 'agent', 'note': 'Program acceptance.', 'acceptance': record(assess(service.build, run, p))}
               for p in run['page_revisions']]
    service.build.finalize({'run_id': run['run_id'], 'actor': 'agent', 'expected_revision': 1, 'reviews': reviews})
    result = short_formal_build.delivery(vault, run['run_id'], grant)
    assert not result['blockers'] and all(p['published'] for p in result['candidates'])
    assert result['knowledge_navigation']['state'] == 'ready'
    assert {g['reason'] for g in result['knowledge_navigation']['gaps']} == {'optional_entry_not_in_qualified_release'}
    assert len(result['card_navigation']) == 3
    index = vault / INDEX
    first = index.read_bytes()
    text = unquote(first.decode('utf-8'))
    for name in ('按类型', '按主题', '按来源', '按项目', '北岸工程', '专题与独立条目'):
        assert name in text
    assert f'../{paths[entry]}' in text and '未交付条目' not in text
    assert all((vault / path).read_bytes() == bodies[pid] for pid, path in paths.items())

    release_path = service._manifest_path(result['release_id'])
    before = release_path.read_bytes()
    release = json.loads(before)
    assert refresh(service, release)['changed'] is False
    assert refresh(service, release, apply=True)['changed'] is False
    assert index.read_bytes() == first
    index.write_bytes(first + b'\nManual note\n')
    assert refresh(service, release, apply=True)['state'] == 'manual_edit'
    assert index.read_bytes().endswith(b'Manual note\n')
    assert release_path.read_bytes() == before

    def unavailable(*args, **kwargs):
        raise OSError('view is temporarily unavailable')

    monkeypatch.setattr(short_formal_build, 'refresh', unavailable)
    replay = short_formal_build.delivery(vault, run['run_id'], grant, replay=True)
    assert replay['release_id'] == result['release_id'] and not replay['blockers']
    assert replay['knowledge_navigation']['state'] == 'deferred'
