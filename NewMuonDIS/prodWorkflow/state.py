"""Atomic state, immutable attempts and inexpensive file identity checks."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from prodWorkflow.config import signature
from productionState import state_lock


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(path):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    if not path.is_file() or info.st_size == 0:
        raise ValueError(f'Missing or empty file: {path}')
    return dict(path=str(path), size=info.st_size, mtime_ns=info.st_mtime_ns)


def verify(files):
    for item in files:
        if fingerprint(item['path']) != item:
            raise ValueError(f'Checked file changed: {item["path"]}; run the upstream checker again')


class State:
    def __init__(self, settings):
        self.path = Path(settings['step_dir']) / 'state.json'
        self.settings = settings
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get('version') != 1 or self.data.get('configuration') != signature(settings):
                raise ValueError(f'Settings differ from {self.path}; use the original YAML or a new production/name')
        else:
            self.data = dict(version=1, id=uuid.uuid4().hex, configuration=signature(settings),
                             groups=[], attempts=[], revision=0)

    def save(self):
        self.data['updated'] = timestamp()
        write_json(self.path, self.data)

    def add_groups(self, groups):
        known = {group['id']: group for group in self.data['groups']}
        for group in groups:
            if group['id'] in known and known[group['id']] != group:
                raise ValueError(f'Inputs changed for planned group {group["id"]}; use a new production/name')
            if group['id'] not in known:
                self.data['groups'].append(deepcopy(group))
                known[group['id']] = group
                self.data['revision'] += 1

    def attempts(self, unit):
        return [attempt for attempt in self.data['attempts'] if unit in attempt['units']]

    def marker(self, attempt):
        return f'[prodWorkflow:{self.data["id"]}:{attempt["id"]}]'

    def refresh(self, registry):
        for attempt in self.data['attempts']:
            attempt['refreshed'] = timestamp()
            try:
                job = registry(attempt['job_id'])
                if self.marker(attempt) not in job.comment:
                    raise ValueError('Ganga job identity differs from the saved workflow')
                children = {child.id: child for child in job.subjobs}
                for unit, child in attempt['units'].items():
                    target = job if child is None else children.get(child)
                    attempt['statuses'][unit] = target.status if target is not None else (
                        job.status if job.status in ('failed', 'killed') else 'unknown')
                attempt.pop('error', None)
            except Exception as error:
                attempt['statuses'] = dict.fromkeys(attempt['units'], 'unknown')
                attempt['error'] = str(error)

    def status(self, unit):
        attempts = self.attempts(unit)
        if not attempts:
            return 'pending'
        # Never race an old attempt that was manually resubmitted.
        for attempt in attempts:
            status = attempt['statuses'].get(unit, 'unknown')
            if status not in ('completed', 'failed', 'killed'):
                return status
        return attempts[-1]['statuses'][unit]

    def checked(self):
        path = self.path.with_name('checked.json')
        if not path.exists():
            return None
        result = json.loads(path.read_text())
        if result.get('state_id') != self.data['id'] or result.get('revision') != self.data['revision']:
            return None
        return result
