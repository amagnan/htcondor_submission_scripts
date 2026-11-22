"""Plan from checked exports, never by scanning a downstream production tree."""

import hashlib
from itertools import islice
import json
from pathlib import Path

from prodWorkflow.gangaProdScript import executable_options
from prodWorkflow.config import EXECUTABLES, PARENTS
from prodWorkflow.state import digest, fingerprint, verify


def upstream(settings, verify_files=True):
    folder = Path(settings['campaign_dir']) / settings['upstream']
    try:
        checked = json.loads((folder / 'checked.json').read_text())
        state = json.loads((folder / 'state.json').read_text())
    except FileNotFoundError:
        raise ValueError(f'Run check for {settings["upstream"]} first; expected {folder / "checked.json"}') from None
    if (checked.get('version') != 1 or checked.get('state_id') != state['id']
            or checked.get('revision') != state['revision']):
        raise ValueError(f'Upstream checker results are stale: {folder}; rerun check')
    if checked['step'] not in PARENTS[settings['step']] or checked['test'] != settings['test']:
        raise ValueError('Upstream step or test/production mode does not match')
    if settings['require_complete'] and not checked['complete']:
        raise ValueError(f'{settings["upstream"]} is incomplete; check/retry it, or set require_complete: false explicitly')
    if not checked['records']:
        raise ValueError(f'No checker-approved inputs in {folder / "checked.json"}')
    if verify_files:
        for record in checked['records']:
            verify(record['files'])
    return checked


def ranges(settings, entries):
    first = settings['first_event']
    if first > entries:
        raise ValueError(f'first_event {first} exceeds {entries} checked entries')
    available = entries - first
    requested = settings['events_per_file']
    count = available if requested == -1 else min(available, requested)
    if settings['test']:
        count = min(count, 2 if settings['step'].startswith('disreplay') else 100)
        return [(first, count)]
    size = settings['events_per_subjob']
    pieces = settings['n_subjobs']
    if size is not None:
        return [(start, min(size, first + count - start)) for start in range(first, first + count, size)] or [(first, 0)]
    pieces = min(pieces or 1, count or 1)
    quotient, remainder = divmod(count, pieces)
    result = []
    for index in range(pieces):
        length = quotient + (index < remainder)
        result.append((first, length))
        first += length
    return result


def analysis_inputs(settings, records, state=None):
    """Group unassigned checked masters, retaining their IDs and file identities."""
    assigned = {parent for group in state.data['groups']
                for parent in group['inputs'].get('parents', [group['parent']])} if state is not None else set()
    records = sorted((record for record in records if record['id'] not in assigned), key=lambda record: record['id'])
    size = settings['masters_per_job']
    if size == -1:
        size = len(records) or 1
    seen = set()
    for offset in range(0, len(records), size):
        batch = records[offset:offset + size]
        if len({record['replay_type'] for record in batch}) != 1:
            raise ValueError('Cannot group analysis inputs with different replay types')
        sources = [source for record in batch for source in record['sources']]
        for source in sources:
            if source in seen:
                raise ValueError(f'Duplicate analysis source: {source}')
            seen.add(source)
        parents = [record['id'] for record in batch]
        yield dict(id=digest(parents), parents=parents, sources=sources,
                   files=[item for record in batch for item in record['files']],
                   geometries=[path for record in batch for path in record.get('geometries', [])],
                   entries=sum(record['entries'] for record in batch), replay_type=batch[0]['replay_type'])


def plan(settings, state=None):
    step = settings['step']
    limit = 1 if settings['test'] else settings['n_jobs']
    known = {group['id'] for group in state.data['groups']} if state is not None else set()
    if state is not None and limit != -1:
        limit -= len(select(state))
    if limit == 0:
        return []
    if step == 'disgen':
        directory = Path(settings['input_dir'])
        if not directory.is_dir():
            raise ValueError(f'Not an input directory: {directory}')
        inputs = (dict(id=digest(str(path.resolve())), sources=[str(path.resolve())],
                       entries=settings['first_event'] + settings['events_per_file'])
                  for path in sorted(directory.rglob(settings['input_pattern']))
                  if not path.name.endswith('_rec.root') and '_backup' not in path.stem and path.is_file())
    else:
        inputs = upstream(settings, verify_files=False)['records']
    if settings['test']:
        inputs = islice(inputs, 1)
    if step == 'analysis' and settings['masters_per_job'] != 1:
        inputs = analysis_inputs(settings, inputs, state)
    groups = []
    found = False
    for record in inputs:
        found = True
        group_id = digest([settings['name'], record['id']])
        if group_id in known:
            continue
        if step.startswith('disreplay') and record['entries'] == 0:
            print(f'Skipping empty checked input {record["id"]}')
            continue
        if step == 'disgen':
            record['files'] = [fingerprint(record['sources'][0])]
        else:
            verify(record['files'])
        group = dict(id=group_id, parent=record['id'], inputs=record, units=[])
        if step == 'replayfilter':
            if settings['first_event'] != 0 or settings['n_subjobs'] or settings['events_per_subjob']:
                raise ValueError('replayfilter requires one unsplit chain starting at entry 0')
            selected_ranges = ranges(settings, record['entries'])
        else:
            selected_ranges = ranges(settings, record['entries'])
        for first, count in selected_ranges:
            unit_id = digest([group_id, first, count])
            stem = f'muonDis_{settings["tag"]}_{group_id}_evt{first}_{first + count}'
            spec = dict(step=step, fs_install=settings['fs_install'], sources=record['sources'],
                        files=record['files'], first=first, count=count)
            if step == 'analysis':
                spec['analysis'] = dict(settings['analysis'], type=record['replay_type'])
                # An explicit override wins; otherwise use the checked companion geometries.
                spec['output'] = f'analysis_evt{first}_{first + count}'
                spec['outputs'] = [spec['output'] + '.tar.gz']
                spec['runfile'] = EXECUTABLES[step]
                spec['args'] = ['analysis-config.json', '--first-entry', str(first),
                                '--n-events', str(count), '--output-dir', spec['output']]
            elif step == 'replayfilter':
                paths = record['sources']
                geometry = settings['geometry_file']
                spec['filter'] = dict(sources=paths, geometry_files=[geometry] * len(paths) if geometry else record['geometries'],
                                      master=record['master'], test=settings['test'],
                                      diagnostics=settings['diagnostics'])
                if 'cross_section' in settings:
                    spec['filter']['cross_section'] = settings['cross_section']
                spec['filter']['master_digest'] = hashlib.sha256(json.dumps(paths).encode()).hexdigest()
                spec['output'] = stem + '_' + record['replay_type'] + '_filtered.root'
                spec['outputs'] = [spec['output'], 'geo_' + spec['output'], str(Path(spec['output']).with_suffix('.json'))]
                spec['runfile'] = 'replayfilter_worker.py'
                spec['args'] = ['-s', '0', '-n', str(count), '-o', spec['output']]
            else:
                spec['runfile'] = EXECUTABLES[step]
                options = executable_options(dict(settings, step=step))
                source = record['sources'][0]
                if step.startswith('disreplay'):
                    tag = stem + '_' + step
                    spec['outputs'] = [f'{prefix}_{tag}.root' for prefix in ('sim', 'params', 'geo')]
                    spec['output'] = spec['outputs'][0]
                    spec['args'] = ['-f', source, *options, '-i', str(first), '-n', str(count), '-o', '.', '--tag', tag]
                else:
                    spec['output'] = stem + '_' + step + '.root'
                    spec['outputs'] = [spec['output']]
                    spec['args'] = ['-f', source, *options, '-s', str(first), '-n', str(count), '-o', spec['output']]
            group['units'].append(dict(id=unit_id, spec=spec))
        groups.append(group)
        if limit != -1 and len(groups) >= limit:
            break
    if step == 'disgen' and not found:
        raise ValueError(f'No inputs matching {settings["input_pattern"]} in {directory}')
    return groups


def select(state, retry=False):
    checked = state.checked()
    approved = {item['unit'] for item in checked['successful']} if checked else set()
    rejected = {item['unit'] for item in checked['problems']} if checked else set()
    selected = []
    for group in state.data['groups']:
        units = []
        for unit in group['units']:
            key = unit['id']
            status = state.status(key)
            if key in approved:
                continue
            if not retry and status == 'pending':
                units.append(unit)
            elif retry and status in ('failed', 'killed', 'completed') and key in rejected:
                units.append(unit)
        if units:
            selected.append(dict(group, units=units))
    limit = 1 if state.settings['test'] else state.settings['n_jobs']
    return selected if limit == -1 else selected[:limit]
