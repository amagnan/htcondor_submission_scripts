"""Persistent production plans and Ganga attempts; no ROOT or Ganga imports."""

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import uuid


VERSION = 1
TERMINAL = {'completed', 'failed', 'killed'}
MUTABLE_SETTINGS = {'n_jobs', 'max_runtime', 'site', 'skip_existing',
                    'resubmit_incomplete', 'retry_failed', 'state_file', 'allow_partial'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def configuration(settings, test):
    # Paths are stored as strings; resource limits and batch/retry policy may change.
    result = {key: value for key, value in settings.items() if key not in MUTABLE_SETTINGS}
    return json.loads(json.dumps(dict(result, test=test), default=str, sort_keys=True))


def range_key(paths):
    return hashlib.sha256(json.dumps([str(path) for path in paths]).encode()).hexdigest()


@contextmanager
def state_lock(path):
    """An atomic directory lock also works across hosts sharing an AFS directory."""
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'Production state is locked: {lock}. Check its owner.json; '
                         'remove the lock directory only after that launcher has stopped.') from None
    try:
        (lock / 'owner.json').write_text(json.dumps(
            dict(host=socket.gethostname(), pid=os.getpid(), started=timestamp()), indent=2))
        yield
    finally:
        (lock / 'owner.json').unlink(missing_ok=True)
        lock.rmdir()


class ProductionState:
    def __init__(self, path, settings, test=False):
        self.path = Path(path)
        self.live = False
        if self.path.exists():
            with self.path.open() as stream:
                self.data = json.load(stream)
            if not isinstance(self.data, dict) or self.data.get('version') != VERSION:
                raise ValueError(f'Unsupported production state: {self.path}')
            expected = configuration(settings, test)
            actual = self.data.get('configuration', {})
            if actual != expected:
                changed = sorted(key for key in actual.keys() | expected.keys()
                                 if actual.get(key) != expected.get(key))
                raise ValueError(f'Production settings differ from saved plan: {", ".join(changed)}. '
                                 'Use the original settings or a separate state and output namespace.')
            if not isinstance(self.data.get('plans'), list) or not isinstance(self.data.get('attempts'), list):
                raise ValueError(f'Invalid production state: {self.path}')
        else:
            self.data = dict(version=VERSION, id=uuid.uuid4().hex, created=timestamp(),
                             configuration=configuration(settings, test), repository=None,
                             plans=None, attempts=[])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data['updated'] = timestamp()
        # Never expose a half-written manifest to another preview.
        descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(self.data, stream, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def set_plans(self, settings, plans, output_names):
        stored = []
        for plan in plans:
            plan = deepcopy(plan)
            keys = []
            for index, chunk in enumerate(plan['subjobs']):
                paths = [settings['output_dir'] / plan['output_format'].format(sjid=index, fname=name)
                         for name in output_names(settings['step'], chunk)]
                keys.append(range_key(paths))
            plan['range_keys'] = keys
            stored.append(plan)
        self.data['plans'] = json.loads(json.dumps(stored, default=str))
        return self.plans(settings)

    def plans(self, settings):
        if self.data['plans'] is None:
            return None
        plans = deepcopy(self.data['plans'])
        for plan in plans:
            plan['source'] = Path(plan['source'])
            plan['sources'] = [Path(source) for source in plan['sources']]
            # Site is a runtime setting, not part of the frozen physics configuration.
            plan['args'][plan['args'].index('--site') + 1] = settings['site']
        return plans

    def bind_repository(self, repository):
        saved = self.data['repository']
        if saved is not None and saved != repository:
            raise ValueError('This production state belongs to a different Ganga repository')
        self.data['repository'] = repository

    def marker(self, attempt):
        return f'[production-state:{self.data["id"]}:{attempt["id"]}]'

    def refresh(self, registry, repository):
        """Read Ganga's current repository statuses; unknown is never retryable."""
        self.bind_repository(repository)
        self.live = True
        self.registry = registry
        registered = set(registry.ids())
        for attempt in self.data['attempts']:
            if attempt['phase'] == 'not_submitted':
                continue
            try:
                if attempt['job_id'] not in registered:
                    statuses = {key: 'missing' for key in attempt['ranges']}
                else:
                    job = registry(attempt['job_id'])
                    if self.marker(attempt) not in job.comment:
                        raise ValueError('Ganga job identity does not match the saved attempt')
                    children = {child.id: child for child in job.subjobs}
                    statuses = {}
                    for key, child_id in attempt['ranges'].items():
                        if child_id is None:
                            status = job.status
                        elif child_id in children:
                            status = children[child_id].status
                        elif job.status in ('failed', 'killed'):
                            status = job.status
                        else:
                            status = 'unknown'
                        statuses[key] = status
                attempt['statuses'] = statuses
                attempt.pop('refresh_error', None)
            except Exception as error:
                attempt['statuses'] = {key: 'unknown' for key in attempt['ranges']}
                attempt['refresh_error'] = str(error)
            attempt['checked'] = timestamp()

    def range_status(self, key):
        latest = ('pending', '')
        for attempt in self.data['attempts']:
            if key not in attempt['ranges'] or attempt['phase'] == 'not_submitted':
                continue
            status = attempt['statuses'].get(key, 'unknown')
            ref = str(attempt['job_id'])
            child_id = attempt['ranges'][key]
            if child_id is not None:
                ref += f'.{child_id}'
            if attempt.get('released') and status in ('missing', 'new'):
                status = 'failed'
            # Check old attempts too: manually resubmitting an old job must not
            # let a new attempt write concurrently to the same destination.
            if status not in TERMINAL:
                return 'held', f'{ref} ({status})'
            latest = (status, ref)
        return latest

    def counts(self):
        return Counter(self.range_status(key)[0]
                       for plan in self.data['plans'] or [] for key in plan['range_keys'])

    def reserve(self, job, plan):
        attempt = dict(id=uuid.uuid4().hex, job_id=job.id, created=timestamp(),
                       phase='prepared',
                       ranges={key: index if plan['split'] else None
                               for index, key in enumerate(plan['range_keys'])},
                       statuses={key: 'submitting' for key in plan['range_keys']})
        job.comment += ' ' + self.marker(attempt)
        self.data['attempts'].append(attempt)
        self.save()
        return attempt

    def adopt(self, registry, job_ids, settings):
        """Match old jobs by the full worker arguments and physical output layout."""
        if not self.live:
            raise ValueError('--adopt-job requires Ganga repository access')
        plans = self.plans(settings)
        registered = set(registry.ids())
        known = {attempt['job_id'] for attempt in self.data['attempts']}
        additions = []
        for job_id in dict.fromkeys(job_ids):
            if job_id in known or job_id not in registered:
                raise ValueError(f'Cannot adopt Ganga job {job_id}: already tracked or absent')
            job = registry(job_id)
            formats = [item.outputfilenameformat for item in job.outputfiles
                       if hasattr(item, 'outputfilenameformat')]
            if len(formats) != 1:
                raise ValueError(f'Cannot adopt Ganga job {job_id}: expected one output layout')
            matches = []
            for plan in plans:
                if (plan['split'] and list(job.application.args) == plan['args']
                        and formats[0] == plan['output_format']
                        and job.splitter is not None
                        and job.splitter.append
                        and [list(chunk) for chunk in job.splitter.args] == plan['subjobs']):
                    matches.append({key: index for index, key in enumerate(plan['range_keys'])})
                elif not job.subjobs and job.splitter is None:
                    for index, chunk in enumerate(plan['subjobs']):
                        if (list(job.application.args) == plan['args'] + chunk
                                and formats[0] == plan['output_format'].format(sjid=index, fname='{fname}')):
                            matches.append({plan['range_keys'][index]: None})
            if len(matches) != 1:
                raise ValueError(f'Cannot adopt Ganga job {job_id}: arguments/splitting/output layout '
                                 'do not match exactly one saved plan')
            attempt = dict(id=uuid.uuid4().hex, job_id=job_id, created=timestamp(),
                           phase='adopted', ranges=matches[0],
                           statuses={key: 'unknown' for key in matches[0]})
            additions.append((job, attempt))
        # Validate every requested adoption before changing any Ganga comments.
        for job, attempt in additions:
            job.comment += ' ' + self.marker(attempt)
            self.data['attempts'].append(attempt)
            self.save()

    def release(self, job_id):
        if not self.live:
            raise ValueError('--release-job requires Ganga repository access')
        attempts = [item for item in self.data['attempts'] if item['job_id'] == job_id]
        if not attempts:
            raise ValueError(f'Ganga job {job_id} is not recorded in this state')
        for attempt in attempts:
            if not set(attempt['statuses'].values()) <= {'missing', 'new'}:
                raise ValueError(f'Cannot release job {job_id}: only missing or never-submitted '
                                 'Ganga jobs can be released')
        for attempt in attempts:
            attempt['released'] = timestamp()
