#!/usr/bin/env python3
"""Compatibility alias for the default same-workflow node test entry."""
import sys
from start_workflow_test import main

if __name__=='__main__':
    if '--from-node' not in sys.argv:sys.argv.extend(['--from-node','resource-reduce'])
    raise SystemExit(main())
