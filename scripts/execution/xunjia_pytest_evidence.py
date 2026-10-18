"""Small pytest plugin recording actual collection and phase outcomes."""
import json
import os
from pathlib import Path
import tempfile

_nodes = []
_reports = {}
_collection_errors = []
# Loaded with -p before project conftest isolates application configuration.
_evidence_path = Path(os.environ['XUNJIA_PYTEST_EVIDENCE'])


def _save(value):
    path = _evidence_path
    fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _snapshot(complete=False, **extra):
    return {'complete': complete, 'nodeids': list(_nodes),
            'reports': dict(_reports), 'collection_errors': list(_collection_errors), **extra}


def pytest_collection_finish(session):
    _nodes[:] = [item.nodeid for item in session.items]
    _save(_snapshot(mode='collect' if session.config.option.collectonly else 'run'))


def pytest_collectreport(report):
    if report.failed:
        _collection_errors.append({'nodeid': report.nodeid, 'error': str(report.longrepr)})


def pytest_runtest_logreport(report):
    _reports.setdefault(report.nodeid, {})[report.when] = report.outcome
    _save(_snapshot(mode='run'))


def pytest_sessionfinish(session, exitstatus):
    outcomes = {}
    for nodeid in _nodes:
        phases = _reports.get(nodeid, {})
        if 'failed' in phases.values():
            outcomes[nodeid] = 'failed'
        elif 'skipped' in phases.values():
            outcomes[nodeid] = 'skipped'
        elif all(phases.get(phase) == 'passed' for phase in ('setup', 'call', 'teardown')):
            outcomes[nodeid] = 'passed'
        else:
            outcomes[nodeid] = 'incomplete'
    counts = {key: sum(value == key for value in outcomes.values())
              for key in ('passed', 'failed', 'skipped', 'incomplete')}
    _save(_snapshot(True, mode='collect' if session.config.option.collectonly else 'run',
                    exit_code=int(exitstatus), counts=counts, outcomes=outcomes))
