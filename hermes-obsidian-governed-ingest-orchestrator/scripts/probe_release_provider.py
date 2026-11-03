#!/usr/bin/env python3
"""Run exactly one native Provider experiment without enabling production routes."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import provider_probe
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('dispatch','worker'));p.add_argument('--vault',required=True,type=Path);p.add_argument('--request',required=True,type=Path)
    a=p.parse_args()
    try:result=getattr(provider_probe,a.command)(a.vault,a.request)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),file=sys.stderr);return 2
    print(json.dumps(result));return 0
if __name__=='__main__':raise SystemExit(main())
