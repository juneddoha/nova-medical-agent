"""Run the active regression suite with SDK transport and network access blocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch


def blocked(*args, **kwargs):
    raise AssertionError('Network/API access is disabled during offline verification')


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    import nova_sdk
    import run

    details = io.StringIO()
    with contextlib.ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guards.enter_context(patch(target, blocked))
        guards.enter_context(patch.object(nova_sdk.Client, '_request', blocked))
        with contextlib.redirect_stdout(details), contextlib.redirect_stderr(details):
            run.prepare()
            suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_*.py')
            result = unittest.TextTestRunner(stream=details, verbosity=2).run(suite)
    summary = {
        'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'network': 'blocked', 'sdk_transport': 'blocked',
        'prepare': 'passed', 'successful': result.wasSuccessful(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not result.wasSuccessful():
        print(details.getvalue(), file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
