#!/usr/bin/env python3
"""Preview or refresh the derived reader index for one committed release."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units.source_units import _load_json
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from short_navigation import OPTIONAL_ERRORS, refresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--apply', action='store_true', help='write the derived index; default is read-only')
    args = parser.parse_args()
    try:
        service = FileVaultFinalizeService(args.vault)
        service.validate(args.release_id)
        release = _load_json(service._manifest_path(args.release_id))
        result = refresh(service, release, apply=args.apply)
    except OPTIONAL_ERRORS as error:
        print(json.dumps({'state': 'deferred', 'reason': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
