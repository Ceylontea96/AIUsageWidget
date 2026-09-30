"""Shared test switches.

Integration tests start real PowerShell, compiled launchers or git clones and
take about half of a serial run. They are skipped unless AIUSAGE_FULL_TESTS=1,
which a release must set; tests/parallel.py --full sets it:

    py -3 -B -m tests.parallel --full

They are also sensitive to load: a PowerShell that starts slowly on a busy
machine can miss their waits. So the parallel runner gives them a phase of
their own, after the other tests and on fewer workers.
"""
import os
import unittest

FULL = os.environ.get('AIUSAGE_FULL_TESTS') == '1'


def integration(reason):
    """Mark a slow, process-starting test class; skip it unless the full suite was asked for."""
    skip = unittest.skipUnless(FULL, f'{reason}; set AIUSAGE_FULL_TESTS=1 to run')

    def mark(cls):
        cls = skip(cls)
        cls.integration = True
        return cls

    return mark
