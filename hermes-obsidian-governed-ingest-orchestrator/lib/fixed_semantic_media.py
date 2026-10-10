"""Deliver only registered whole images already authorized by a checked packet."""
import base64
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath

from hermes_source_units import ContractError
from hermes_source_units.source_units import _vault_path

MAX_IMAGES = 16
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_TYPES = {'image/png','image/jpeg','image/webp','image/gif'}


def image_budget():
    config=json.loads((Path(__file__).parents[1]/'config/orchestration.json').read_text())
    budget=config.get('semantic_images',{'max_images':MAX_IMAGES,'max_bytes':MAX_IMAGE_BYTES})
    if set(budget)!={'max_images','max_bytes'} or any(type(v)!=int or v<=0 for v in budget.values()):
        raise ContractError('INVALID_SCHEMA','$','semantic image budget requires positive integer limits')
    return budget


def image_usage(images):
    return {'image_count':len(images),'image_bytes':sum(i['bytes'] for i in images),
            'largest_image_bytes':max((i['bytes'] for i in images),default=0),**image_budget()}


def require_images_fit(images):
    if not images_fit(images):
        raise ContractError('SEMANTIC_IMAGE_OVERSIZE','$',json.dumps(image_usage(images),sort_keys=True))


def image_catalog(adapter, packet):
    catalog={};verified={}
    source=adapter.workflow.knowledge.source
    for task in packet['tasks']:
        for material in task.get('materials',[]):
            asset=material.get('asset')
            if not asset: continue
            relative=asset['path']
            if relative in catalog: continue
            parts=PurePosixPath(relative).parts
            if parts[:3]!=('_system','sources','artifacts') or len(parts)<6:
                raise ContractError('ACCESS_DENIED','$','semantic image is not a registered artifact')
            manifest_ref='/'.join(parts[:5])+'/manifest.json'
            if manifest_ref not in verified:
                verified[manifest_ref]=source._verify_artifact(source._artifact_path(manifest_ref))[0]
            manifest=verified[manifest_ref]
            name='/'.join(parts[5:])
            registered=next((a for a in manifest['assets'] if a['path']==name),None)
            if (not registered or manifest['artifact_revision']!=parts[4]
                    or registered['media_type']!=asset['media_type']):
                raise ContractError('SOURCE_CHANGED','$','semantic image registration changed')
            if registered['media_type'] not in IMAGE_TYPES:
                raise ContractError('UNSUPPORTED_SEMANTIC_ASSET','$','whole asset needs a supported semantic content transport')
            path=_vault_path(adapter.workflow.vault,relative)
            if path.stat().st_size>image_budget()['max_bytes']:
                require_images_fit([{'bytes':path.stat().st_size}])
            data=path.read_bytes()
            digest=hashlib.sha256(data).hexdigest()
            if digest!=registered['sha256']:
                raise ContractError('SOURCE_CHANGED','$','semantic image bytes changed')
            catalog[relative]={'sha256':digest,'bytes':len(data),'media_type':registered['media_type'],
                'url':f"data:{registered['media_type']};base64,"+base64.b64encode(data).decode('ascii')}
    return catalog


def select_images(tasks, catalog):
    selected={}
    for task in tasks:
        for material in task.get('materials',[]):
            asset=material.get('asset')
            if not asset: continue
            relative=asset['path']
            if relative not in catalog:
                raise ContractError('UNINSPECTED_SUPPORT','$','authorized image content has not been prepared')
            image=selected.setdefault(relative,{**catalog[relative],'references':[]})
            image['references'].append({'task':task['task'],'material':material['ref']})
    return list(selected.values())


def images_fit(images):
    budget=image_budget()
    return len(images)<=budget['max_images'] and sum(i['bytes'] for i in images)<=budget['max_bytes']


def message_content(text, images):
    if not images: return text
    labels='\n'.join('Image '+str(n)+': '+', '.join(r['task']+'/'+r['material'] for r in i['references'])
                     for n,i in enumerate(images,1))
    return [{'type':'text','text':text+'\n'+labels},
            *[{'type':'image_url','image_url':{'url':i['url'],'detail':'high'}} for i in images]]


def normalize_content(value):
    """Compare Chat/Responses SDK framing, retaining exact text and image bytes."""
    if isinstance(value,str): return (value,[])
    if not isinstance(value,list): return None
    texts=[];images=[]
    for part in value:
        if not isinstance(part,dict): return None
        if part.get('type','text') in ('text','input_text'):
            if set(part)-{'type','text','cache_control'} or not isinstance(part.get('text'),str): return None
            texts.append(part['text'])
        elif part.get('type') in ('image_url','input_image'):
            if set(part)-{'type','image_url','detail','cache_control'}: return None
            url=part.get('image_url');detail=part.get('detail','high')
            if isinstance(url,dict):
                if set(url)-{'url','detail'}: return None
                detail=url.get('detail',detail);url=url.get('url')
            if not isinstance(url,str) or detail!='high': return None
            images.append(url)
        else: return None
    return (''.join(texts),images)
