#!/usr/bin/env python3
"""Explicit test-only entry: reuse pinned conversion Bundles in a fresh Vault."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from watcher_runtime import ensure_native_runtime
from post_mineru_test import import_bundles
from start_vault_workflow import start

def main():
    ensure_native_runtime()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request',required=True,type=Path)
    p.add_argument('--record',required=True,type=Path)
    p.add_argument('--bootstrap-skill',required=True,type=Path)
    a=p.parse_args()
    try:
        q=json.loads(a.request.read_text(encoding='utf-8-sig'))
        if q.get('test_mode')!='post-mineru-test/v1':raise ValueError('explicit post-MinerU test request required')
        print(json.dumps(start(q,a.record,a.bootstrap_skill,before_workflow=import_bundles),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2
    return 0

if __name__=='__main__':raise SystemExit(main())
