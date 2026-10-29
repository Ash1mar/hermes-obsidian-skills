"""Explicit deployment schema assets, separate from Vault dependencies."""
import hashlib
import os
from pathlib import Path
from .validation import ContractError

CONTRACT='hermes-installed-schema/v1'
INSTALL_ROOT=Path('/root/.hermes/skills/domain')


def current_schema():
    from . import validation
    return Path(validation.__file__).absolute().with_name('schemas')/'contracts.json'


def installed_schema_asset():
    path=current_schema()
    if not path.is_relative_to(INSTALL_ROOT):
        raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','schema is not in the supported Skill installation')
    return {'contract':CONTRACT,'root':str(INSTALL_ROOT),'path':str(path)}


def describe(asset):
    if asset is None:return {},{}
    validate(asset)
    path=Path(asset['path'])
    for part in (path,*path.parents):
        if part.is_symlink():raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','schema path contains a symlink')
    value={**asset,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return {str(path):value['sha256']},value


def acquire(asset):
    """One protector owns this deployed inode; a stale seal is never adopted."""
    if asset is None:return None
    import fcntl
    from .evidence_snapshot import flags,IMMUTABLE
    validate(asset);path=Path(asset['path'])
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','another protector owns the deployed schema') from exc
        if flags(path)&IMMUTABLE or flags(path.parent)&IMMUTABLE:
            raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','schema already sealed; revoke or release its prior owner first')
        return descriptor
    except BaseException:os.close(descriptor);raise


def verify_owner(descriptor,path):
    if descriptor is None:return
    owned=os.fstat(descriptor);current=Path(path).lstat()
    if (owned.st_dev,owned.st_ino)!=(current.st_dev,current.st_ino):
        raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','schema inode changed while preparing protection')


def release(descriptor):
    if descriptor is not None:os.close(descriptor)


def validate(asset):
    path=Path(asset.get('path',''));root=Path(asset.get('root',''))
    if (asset.get('contract')!=CONTRACT or not path.is_absolute() or not root.is_absolute()
            or not path.is_relative_to(root) or path!=current_schema()
            or path.name!='contracts.json' or path.parent.name!='schemas'):
        raise ContractError('EVIDENCE_SNAPSHOT_REVOKED','$','invalid deployment schema asset contract')


def release_allowed(vault,target,manifest):
    target=Path(target)
    if target.is_relative_to(vault):return True
    asset=manifest.get('host_schema')
    if not asset:return False
    validate(asset)
    # Production CLI has no user-supplied host root or path. Release only the
    # exact installed schema and its schema directory, never arbitrary parents.
    path=current_schema()
    return asset['root']==str(INSTALL_ROOT) and target in (path,path.parent)
