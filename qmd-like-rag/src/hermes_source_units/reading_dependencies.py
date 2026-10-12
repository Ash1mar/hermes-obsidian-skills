"""Derive required whole assets from explicit links in bounded core evidence.

Section-level associations are navigation, never a license to read all assets.
An immutable supplement preserves existing package/Pass identities while adding
only registered dependencies under the same source and governance authority.
"""
import copy
import re
from pathlib import PurePosixPath
from urllib.parse import unquote

from .validation import ContractError, canonical_json, fingerprint

CONTRACT = 'explicit-registered-reading-dependencies/v1'


def required_reading(source, package):
    access={'actor':package['actor'],'purpose':'construction',
            'registry_revision':package['document_registry_revision']}
    materials=copy.deepcopy(package['materials'])
    known={canonical_json(m['source_ref']['unit_ref']):m for m in materials}
    required={};links=[]
    queue=[m for m in materials if m['role']=='core']
    scanned=set()
    with source.reading_snapshot():
        while queue:
            material=queue.pop(0);ref=material['source_ref'];key=canonical_json(ref)
            if key in scanned:continue
            scanned.add(key)
            live=source.get({'source_ref':ref,'access':access})
            if live['core_text']!=material['core_text'] or live['content_sha256']!=material['unit_content_sha256']:
                raise ContractError('SOURCE_CHANGED','$','dependency parent differs from canonical reading')
            text=live['core_text'] or ''
            parent=ref['unit_ref']
            _,units,_=source._load_set(parent['resource_id'],parent['unit_set_id'])
            assets=[u for u in units if u['locator']['kind']=='asset']
            # Local destinations are compared with the registered path. No path
            # traversal, file search, external retrieval or semantic guessing.
            destinations={unquote(p.strip('<>')) for p in re.findall(r'\]\(\s*([^\s)]+)',text)}
            for asset in assets:
                path=asset['locator']['path']
                identifiers=[a['asset_id'] for a in live['asset_refs'] if a['path']==path]
                explicit=(path in destinations or path in text or any(
                    re.search(r'(?:table-id|block-id):\s*'+re.escape(i)+r'(?:\s|;|-->)',text)
                    for i in identifiers))
                label=re.fullmatch(r'table_([0-9]+(?:_[0-9]+)+)',PurePosixPath(path).stem)
                if label:
                    number=r'[.．]'.join(map(re.escape,label[1].split('_')))
                    explicit=explicit or bool(re.search(r'(?:表|\bTable)\s*'+number+r'(?![0-9.．])',text,re.IGNORECASE))
                if not explicit:continue
                if asset['ref']['artifact_revision']!=parent['artifact_revision']:
                    raise ContractError('SOURCE_CHANGED','$','dependency artifact differs from parent')
                dep={'unit_ref':copy.deepcopy(asset['ref']),'span':None}
                depkey=canonical_json(asset['ref'])
                links.append({'parent':copy.deepcopy(ref),'dependency':dep,'reason':'explicit registered asset reference'})
                if depkey in required:continue
                required[depkey]=dep
                if depkey in known:continue
                value=source.get({'source_ref':dep,'access':access})
                selected=value['core_text']
                import hashlib
                item={'role':'context','source_ref':dep,'unit_content_sha256':value['content_sha256'],
                    'selected_sha256':hashlib.sha256(selected.encode('utf-8')).hexdigest() if selected is not None else None,
                    'core_text':selected,'asset_refs':value['asset_refs'],'quality_refs':value['quality_refs'],
                    'section_id':value['section_id'],'heading_path':value['heading_path']}
                materials.append(item);known[depkey]=item;queue.append(item)
        supplement={'contract':CONTRACT,'base_package_id':package['package_id'],
            'task_id':package['task_id'],'actor':package['actor'],
            'registry_revision':package['document_registry_revision'],
            'required_refs':list(required.values()),'links':links,
            'materials':[m for m in materials if canonical_json(m['source_ref']['unit_ref']) in required]}
        supplement['supplement_id']=fingerprint(supplement)
    return materials,supplement
