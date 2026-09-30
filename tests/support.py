"""Shared test switches.

Integration tests start real PowerShell, compiled launchers or git clones and
take about half of a full run. They are skipped unless AIUSAGE_FULL_TESTS=1,
which a release must set:

    set AIUSAGE_FULL_TESTS=1 && py -3 -B -m unittest discover -p "test_*.py"
"""
import os
import unittest

FULL = os.environ.get('AIUSAGE_FULL_TESTS') == '1'


def integration(reason):
    """Skip a slow, process-starting test unless the full suite was asked for."""
    return unittest.skipUnless(FULL, f'{reason}; set AIUSAGE_FULL_TESTS=1 to run')
