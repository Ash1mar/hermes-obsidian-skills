#!/usr/bin/env python3
"""Compare published evidence on two hash-identical registered source inventories."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units import FileSourceUnitService
from hermes_source_units.source_units import _vault_path
from hermes_source_units.validation import fingerprint, validate_record
from short_evidence_pass import require


def scope(vault, workflow, manifest_ref):
    workflow_path = _vault_path(vault, f'_system/ledgers/ingest-workflows/{workflow}.json')
    ledger = json.loads(workflow_path.read_bytes())
    require(ledger['workflow_id'] == workflow, 'workflow identity changed')
    require(all(row['status'] == 'ready' for row in ledger['source_outcomes']),
            'compare only fully registered, settled source inventories')
    source = FileSourceUnitService(vault)
    inventory, by_unit = {}, {}
    for row in ledger['source_outcomes']:
        resource, units = row['resource_id'], row['unit_set_id']
        source.validate(resource, units)
        _, records, _ = source._load_set(resource, units)
        for unit in records:
            loc = unit['locator']
            material = {'source_sha256': row['content_sha256'], 'kind': loc['kind'], 'path': loc['path'],
                        'span': loc.get('span'), 'content_sha256': unit['content_sha256']}
            key = fingerprint(material)
            require(key not in inventory, 'ambiguous registered material identity')
            inventory[key] = material
            by_unit[(resource, units, unit['ref']['unit_id'])] = key

    manifest = _vault_path(vault, manifest_ref)
    payload = manifest.read_bytes()
    release = json.loads(payload)
    validate_record('knowledge_release', release)
    require(manifest.parts[-4:] == ('_system', 'knowledge-releases', release['release_id'], 'manifest.json')
            and release['state'] == 'completed', 'comparison requires the exact committed release manifest path')
    release_root = manifest.parents[3]
    eligible = {item['id'] for item in release['index_eligibility']
                if item['eligible'] and item['kind'] == 'knowledge_page'}
    cited = defaultdict(list)
    for page in release['subjects']:
        if page['page_id'] not in eligible:
            continue
        revision = f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['current_revision_id']}.json"
        sidecar = _vault_path(release_root, revision)
        if not sidecar.is_file():
            sidecar = _vault_path(vault, revision)
        saved = json.loads(sidecar.read_bytes())
        require(saved['page_id'] == page['page_id'] and saved['revision_id'] == page['current_revision_id']
                and saved['path'] == page['current_path'] and any(
                    body.is_file() and hashlib.sha256(body.read_bytes()).hexdigest() == saved['authored_sha256']
                    for body in {_vault_path(root, page['current_path']) for root in (release_root, vault)}),
                'published page bytes do not match their committed revision')
        for support in page['active_support_refs']:
            unit = support['unit_ref']
            key = by_unit.get((unit['resource_id'], unit['unit_set_id'], unit['unit_id']))
            require(key is not None, 'released support is outside the selected source inventory')
            cited[key].append({'path': page['current_path'], 'span': support.get('span'),
                               'page_revision_id': page['current_revision_id']})
    summary = {'release_id': release['release_id'], 'manifest_sha256': hashlib.sha256(payload).hexdigest(),
               'pages': len(eligible), 'cited_units': len(cited),
               'cited_assets': sum(inventory[key]['kind'] == 'asset' for key in cited)}
    return summary, inventory, cited


def compare(baseline, candidate):
    before, old_inventory, old_cited = baseline
    after, inventory, cited = candidate
    require(inventory == old_inventory and inventory, 'registered material inventories differ')
    changed = sorted(old_cited.keys() | cited.keys())
    return {'baseline': before, 'candidate': after, 'registered_units': len(inventory),
            'registered_assets': sum(item['kind'] == 'asset' for item in inventory.values()),
            'material_comparison': [{**inventory[key], 'baseline_pages': old_cited.get(key, []),
                                     'candidate_pages': cited.get(key, [])} for key in changed],
            'semantic_review': 'required separately for retained facts, limits, table cells and exclusions'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ('baseline', 'candidate'):
        parser.add_argument(f'--{side}-vault', type=Path, required=True)
        parser.add_argument(f'--{side}-workflow', required=True)
        parser.add_argument(f'--{side}-manifest', required=True, help='Vault-relative release manifest')
    args = parser.parse_args()
    baseline = scope(args.baseline_vault.resolve(), args.baseline_workflow, args.baseline_manifest)
    candidate = scope(args.candidate_vault.resolve(), args.candidate_workflow, args.candidate_manifest)
    print(json.dumps(compare(baseline, candidate), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
