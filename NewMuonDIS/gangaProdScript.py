#!/usr/bin/env python3
"""Prepare one stage of a DIS production from YAML, optionally submitting with Ganga."""

import argparse
import base64
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

import yaml


# Ganga executes this file in GangaCore.GPI's namespace, not as __main__.
IN_GANGA = all(name in globals() for name in ('config', 'Job', 'Executable', 'File',
                                             'ArgSplitter', 'MassStorageFile', 'Condor', 'FileChecker'))
# In that namespace __file__ belongs to GPI; Ganga puts this script in argv[0].
SCRIPT_DIR = Path(sys.argv[0] if IN_GANGA else __file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from productionState import ProductionState, state_lock


DATA_STEPS = ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter')
STEPS = (*DATA_STEPS, 'replayfilter', 'analyse')
DEFAULTS = {
    'tag': 'TRY2026',
    'production': 261009,
    'output_base': '/eos/experiment/ship/user/ammagnan/MuonDIS',
    'output_dir': None,  # Otherwise OUTPUT_BASE/TAG/PRODUCTION.
    'input_dir': None,
    'input_pattern': None,
    'fs_install': None,
    'site': 'CERN',
    'max_runtime': 86000,
    'n_jobs': -1,  # Maximum new Ganga jobs after skipping outputs; -1 selects all.
    'files_per_job': 1,  # Analysis only: consecutive inputs in one chain.
    'masters_per_job': None,  # Replay analysis: complete filter masters per chain.
    'replay_type': 'disreplay_gen',
    'geometry_file': None,  # Replay filter override; otherwise each source's sibling.
    'skip_existing': True,
    'resubmit_incomplete': False,
    'state_file': None,  # Optional persistent plan and Ganga submission history.
    'retry_failed': False,
    'n_subjobs': None,
    'events_per_file': None,
    'events_per_subjob': None,
    'first_event': 0,
    'executable': None,
    'options': {},
    'analysis': {},  # analyseEvents YAML settings; input/range/output are managed here.
    'analysis_name': 'default',
}
REPLAY_OPTIONS = {
    '--NewMuDIS': True, '--shieldName': 'TRY_2026',
    '--field_map': 'files/2026_05_07_MainSpectrometerField_V21_3000.root',
    '--strawDesign': 10, '--noSND': True, '--debug': 0, '--seed': 0,
}
STEP_DEFAULTS = {
    'replayfilter': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'newMuonDIS/replayFilter.py',
        'input_pattern': 'sim_*_disreplay_gen.root',
        'events_per_file': -1,
        'options': {},
    },
    'analyse': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'newMuonDIS/analyseEvents.py',
        'input_pattern': 'sim_*_disreplay_gen.root',
        'events_per_file': -1,
        'options': {},
    },
    'disgen': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'newMuonDIS/prepareEvents.py',
        'input_dir': '/eos/experiment/ship/simulation/cuda_muons/try_2026_MS5',
        'input_pattern': '*sim_*.root',
        'events_per_file': 250000,
        'options': {
            '--nDIS': 100,
            '--geoFile': '/eos/experiment/ship/simulation/cuda_muons/try_2026_MS5/geofile_2609.root',
            '--z_max': 9500, '--z_min': 2000, '--debug': 0,
        },
    },
    'disfilter': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'newMuonDIS/filterEvents.py',
        'input_pattern': '*_disgen.root',
        'events_per_file': -1,
        'options': {
            '--filter-option': 1, '--pythia-decays': True,
            '--geoFile': '/eos/experiment/ship/simulation/cuda_muons/try_2025/processed_muons/geo_cuda_test.root',
            '--field-map': '/afs/cern.ch/work/a/ammagnan/FairShip/files/2026_05_07_MainSpectrometerField_V21_3000.root',
            '--muon-shield-field-map': '/afs/cern.ch/work/a/ammagnan/FairShip/files/TRY_2025.root',
        },
    },
    'disreplay_gen': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'macro/run_simScript.py',
        'input_pattern': '*_disgen.root',
        'events_per_file': -1,
        'options': REPLAY_OPTIONS,
    },
    'disreplay_filter': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
        'executable': 'macro/run_simScript.py',
        'input_pattern': '*_disfilter.root',
        'events_per_file': -1,
        'options': REPLAY_OPTIONS,
    },
}


def load_settings(filename):
    with open(filename) as stream:
        supplied = yaml.safe_load(stream)
    if not isinstance(supplied, dict) or supplied.get('step') not in STEPS:
        raise ValueError('YAML must contain step: ' + ', '.join(STEPS))
    unknown = supplied.keys() - (DEFAULTS.keys() | {'step'})
    if unknown:
        raise ValueError(f'Unknown YAML settings: {sorted(unknown, key=str)}')
    step = supplied['step']
    settings = deepcopy(DEFAULTS)
    settings.update(deepcopy(STEP_DEFAULTS[step]))
    options = settings['options'].copy()
    if not isinstance(supplied.get('options', {}), dict):
        raise ValueError('options must be a mapping of executable flags to values')
    options.update(supplied.get('options', {}))
    settings.update(supplied)
    settings['options'] = options
    if settings['replay_type'] not in ('disreplay_gen', 'disreplay_filter'):
        raise ValueError('replay_type must be disreplay_gen or disreplay_filter')
    if step == 'replayfilter' and 'input_pattern' not in supplied:
        settings['input_pattern'] = 'sim_*_' + settings['replay_type'] + '.root'
    for key in ('skip_existing', 'resubmit_incomplete', 'retry_failed'):
        if type(settings[key]) is not bool:
            raise ValueError(f'{key} must be true or false')
    if step == 'analyse':
        if not isinstance(settings['analysis'], dict):
            raise ValueError('analysis must be an analyseEvents settings mapping')
        analysis = {'type': 'disreplay_gen', 'save_plots': False, 'weight_factor': 3.6e-6}
        analysis.update(settings['analysis'])
        if analysis['type'] not in DATA_STEPS:
            raise ValueError('analysis.type must be one of ' + ', '.join(DATA_STEPS))
        reserved = {'production_path', 'first_entry', 'n_events', 'output_dir'} & analysis.keys()
        if reserved:
            raise ValueError(f'Use top-level input/output/range settings instead of analysis: {sorted(reserved)}')
        geometry = analysis.get('geometry_file')
        if geometry is not None:
            if not isinstance(geometry, str) or not geometry:
                raise ValueError('analysis.geometry_file must be a nonempty path or null')
            analysis['geometry_file'] = str(Path(geometry).expanduser().resolve())
        settings['analysis'] = analysis
        name = settings['analysis_name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('analysis_name must be a simple directory name')
        if 'input_pattern' not in supplied:
            prefix = 'sim_' if analysis['type'].startswith('disreplay') else 'muonDis_'
            settings['input_pattern'] = prefix + '*_' + analysis['type'] + '.root'
            if settings['masters_per_job'] is not None:
                settings['input_pattern'] = '*_' + analysis['type'] + '_filtered.root'

    if step == 'disgen' and settings['n_subjobs'] is None and settings['events_per_subjob'] is None:
        settings['events_per_subjob'] = 25000
    for key in ('fs_install', 'executable', 'input_pattern', 'site', 'tag'):
        if not isinstance(settings[key], str) or not settings[key]:
            raise ValueError(f'{key} must be a nonempty string')
    if Path(settings['tag']).name != settings['tag'] or settings['tag'] in ('.', '..'):
        raise ValueError('tag must be a single directory name')
    if str(settings['production']) in ('.', '..') or '/' in str(settings['production']):
        raise ValueError('production must be a single directory name or number')
    output = settings['output_dir']
    if output is None:
        output = Path(settings['output_base']) / settings['tag'] / str(settings['production'])
    settings['output_dir'] = Path(output).expanduser().resolve()
    settings['input_dir'] = Path(settings['input_dir'] or settings['output_dir']).expanduser().resolve()
    settings['fs_install'] = str(Path(settings['fs_install']).expanduser().resolve())
    if settings['state_file'] is not None:
        if not isinstance(settings['state_file'], str) or not settings['state_file']:
            raise ValueError('state_file must be a nonempty path or null')
        settings['state_file'] = Path(settings['state_file']).expanduser().resolve()
    return settings


def integer(settings, key, minimum, allow_all=False, optional=False):
    value = settings[key]
    if optional and value is None:
        return value
    if type(value) is not int or not (value >= minimum or allow_all and value == -1):
        raise ValueError(f'{key} must be an integer >= {minimum}' + (' or -1 (all)' if allow_all else ''))
    return value


def event_ranges(settings, test=False):
    """Return disjoint ranges; the last of multiple ranges reads to EOF."""
    first = integer(settings, 'first_event', 0)
    total = integer(settings, 'events_per_file', 1, allow_all=True)
    count = integer(settings, 'n_subjobs', 1, optional=True)
    size = integer(settings, 'events_per_subjob', 1, optional=True)
    if test:
        return [(first, 2 if settings['step'].startswith('disreplay') else 100)]
    if total == -1:
        if size is None:
            if count not in (None, 1):
                raise ValueError('Multiple subjobs require events_per_file or events_per_subjob')
            return [(first, -1)]
        if count is None:
            raise ValueError('With events_per_file: -1, events_per_subjob requires n_subjobs')
        ranges = [(first + i * size, size) for i in range(count)]
    elif size is not None:
        needed = (total + size - 1) // size
        if count is not None and count != needed:
            raise ValueError('n_subjobs disagrees with events_per_file/events_per_subjob')
        ranges = [(first + offset, min(size, total - offset)) for offset in range(0, total, size)]
    else:
        count = count or 1
        if count > total:
            raise ValueError('n_subjobs cannot exceed events_per_file')
        quotient, remainder = divmod(total, count)
        ranges = []
        for i in range(count):
            size = quotient + (i < remainder)
            ranges.append((first, size))
            first += size
    if len(ranges) > 1:
        ranges[-1] = (ranges[-1][0], -1)
    return ranges


def executable_options(settings):
    # These flags belong to the planner; overriding them would break splitting/naming.
    reserved = {'-f', '--inputfile', '--inputFile', '-o', '--output', '--outputfile',
                '-n', '--n_events', '--nEvents', '--tag', '-i', '--firstEvent',
                '--start_event', '--first-entry', '--n-events', '--output-dir'}
    if settings['step'] == 'analyse' and settings['options']:
        raise ValueError('Use the analysis mapping for analyseEvents settings, not options')
    if settings['step'] in ('disgen', 'disfilter'):
        reserved.add('-s')
    options = settings['options']
    if options.get('--SND'):
        options = dict(options, **{'--noSND': False})
    result = []
    for flag, value in options.items():
        if not isinstance(flag, str) or not flag.startswith('-') or '=' in flag or any(c.isspace() for c in flag):
            raise ValueError(f'Invalid executable option: {flag!r}')
        if flag in reserved or (not flag.startswith('--') and len(flag) != 2):
            raise ValueError(f'{flag} is reserved; use input/output and event settings instead')
        if value is None or value is False:
            continue
        result.append(flag)
        if value is not True:
            values = value if isinstance(value, list) else [value]
            if any(type(item) not in (str, int, float) for item in values):
                raise ValueError(f'{flag} must have scalar values')
            result.extend(str(item) for item in values)
    return result


def production_stem(source):
    for step in STEPS:
        if source.stem.endswith('_' + step):
            return source.stem[:-len(step) - 1]
    return source.stem


def output_names(step, chunk):
    if step == 'replayfilter':
        name = chunk[-1]
        return [name, 'geo_' + name.removeprefix('sim_'), str(Path(name).with_suffix('.json'))]
    if step == 'analyse':
        return [chunk[-1] + '.tar.gz']
    if step.startswith('disreplay'):
        return [f'{prefix}_{chunk[-1]}.root' for prefix in ('sim', 'params', 'geo')]
    return [chunk[-1]]


def inspect_output(path):
    """Return a change-detection token, usability, and whether unlink is safe."""
    def token(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, False, True
    state = (token(info),)
    removable = stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        try:
            info = path.stat()
        except FileNotFoundError:
            return state + (None,), False, True
        state += (token(info),)
    return state, stat.S_ISREG(info.st_mode) and info.st_size > 0, removable


def cleanup_incomplete_outputs(plan):
    """Remove only planned file/link outputs, immediately before submission."""
    records = plan.get('cleanup', [])
    # Validate every output, including formerly missing files, before deleting any.
    for record in records:
        for path, expected in record.items():
            state, _, removable = inspect_output(path)
            if state != expected or not removable:
                raise ValueError(f'Output changed since planning; rerun the preview: {path}')
    for record in records:
        for path, expected in record.items():
            if expected is not None:
                print(f'Removing incomplete-range output: {path}', flush=True)
                path.unlink()


def select_pending_jobs(settings, jobs, limit, summary, state=None):
    """Apply the new-job budget after checking each original range destination."""
    selected = []
    for job in jobs:
        if limit != -1 and len(selected) >= limit:
            break
        pending = []
        cleanup = {}
        checks = {}
        tracking_checks = {}
        completed = 0
        for index, chunk in enumerate(job['subjobs']):
            paths = [settings['output_dir'] / job['output_format'].format(sjid=index, fname=name)
                     for name in output_names(settings['step'], chunk)]
            inspected = [inspect_output(path) for path in paths]
            checks[index] = dict(zip(paths, (item[0] for item in inspected)))
            existing = [token is not None for token, _, _ in inspected]
            if state is not None:
                status, ref = state.range_status(job['range_keys'][index])
                tracking_checks[job['range_keys'][index]] = (status, ref)
                if status == 'held' or (status in ('failed', 'killed') and not settings['retry_failed']):
                    category = 'held' if status == 'held' else 'failed'
                    summary[f'skipped_{category}_ranges'] += 1
                    summary['warnings'].append(f'Ganga {ref}: {status}; skipping range for {job["source"]}.')
                    continue
                if (status == 'completed' and not all(usable for _, usable, _ in inspected)
                        and not settings['resubmit_incomplete']):
                    summary['skipped_incomplete_ranges'] += 1
                    summary['warnings'].append(f'Ganga {ref}: completed with absent/empty/partial outputs; '
                                               'use --resubmit-incomplete to retry this range.')
                    continue
            if any(existing):
                if not settings['skip_existing']:
                    raise ValueError(f'Output already exists: {paths[existing.index(True)]}')
                if not all(usable for _, usable, _ in inspected):
                    retry = settings['resubmit_incomplete'] and all(
                        removable for _, _, removable in inspected)
                    action = 'eligible for cleanup and resubmission' if retry else 'skipping range'
                    if settings['resubmit_incomplete'] and not retry:
                        action += ' (contains a directory or special file; not deleting it)'
                    summary['warnings'].append(
                        f'Incomplete output set for {job["source"]}, original range {index}: '
                        + ', '.join(str(path) for path in paths) + f'; {action}.')
                    if retry:
                        cleanup[index] = dict(zip(paths, (state for state, _, _ in inspected)))
                        pending.append(index)
                    else:
                        summary['skipped_incomplete_ranges'] += 1
                    continue
                summary['skipped_ranges'] += 1
                completed += 1
            else:
                pending.append(index)
        if not pending:
            if completed == len(job['subjobs']):
                summary['skipped_inputs'] += len(job['sources'])
            continue
        if len(pending) == len(job['subjobs']):
            selected.append(dict(job, cleanup=list(cleanup.values()), checks=list(checks.values()),
                                 tracking_checks=tracking_checks))
            continue
        # Removing ArgSplitter entries renumbers sjid. Repair each missing range
        # as an unsplit job with the original physical destination instead.
        for index in pending:
            if limit != -1 and len(selected) >= limit:
                break
            selected.append(dict(job, subjobs=[job['subjobs'][index]], split=False,
                                 output_format=job['output_format'].format(sjid=index, fname='{fname}'),
                                 original_range=index, cleanup=[cleanup[index]] if index in cleanup else [],
                                 checks=[checks[index]],
                                 **({'range_keys': [job['range_keys'][index]],
                                     'tracking_checks': {job['range_keys'][index]:
                                                         tracking_checks[job['range_keys'][index]]}}
                                    if state is not None else {})))
    summary['selected_ranges'] = sum(len(job['subjobs']) for job in selected)
    summary['selected_inputs'] = len({source for job in selected for source in job['sources']})
    summary['selected_jobs'] = len(selected)
    summary['requested_jobs'] = limit
    summary['resubmitted_ranges'] = sum(len(job['cleanup']) for job in selected)
    return selected


def plan_jobs(settings, test=False, summary=None, state=None, extend=False):
    integer(settings, 'max_runtime', 1)
    limit = integer(settings, 'n_jobs', 0, allow_all=True)
    files_per_job = integer(settings, 'files_per_job', 1)
    if settings['masters_per_job'] is not None:
        integer(settings, 'masters_per_job', 1)
        if settings['step'] != 'analyse':
            raise ValueError('masters_per_job is only supported for step: analyse')
    if settings['step'] != 'analyse' and files_per_job != 1:
        raise ValueError('files_per_job is only supported for step: analyse')
    if summary is None:
        summary = {}
    summary.update(skipped_ranges=0, skipped_inputs=0, skipped_incomplete_ranges=0,
                   skipped_missing_geometry_inputs=0, skipped_held_ranges=0,
                   skipped_failed_ranges=0, warnings=[])
    if test and limit != 0:
        limit = 1
    if test:
        files_per_job = 1
    ranges = event_ranges(settings, test)
    options = executable_options(settings)
    if settings['step'] == 'analyse' and settings['analysis']['type'].startswith('disreplay'):
        geometry = settings['analysis'].get('geometry_file')
        if geometry and not Path(geometry).is_file():
            raise ValueError(f'Analysis geometry override is not an accessible file: {geometry}. '
                             'Correct analysis.geometry_file or set it to null to use each input’s sibling geometry.')
    saved = state.plans(settings) if state is not None else None
    if saved is not None and not extend:
        return select_pending_jobs(settings, saved, limit, summary, state)
    if settings['step'] == 'replayfilter':
        from replayProduction import plan_filter
        jobs = plan_filter(settings, test, saved or [])
        if state is not None:
            jobs = state.set_plans(settings, jobs, output_names)
        return select_pending_jobs(settings, jobs, limit, summary, state)
    directory = settings['input_dir']
    output = settings['output_dir']
    step = settings['step']
    if not directory.is_dir():
        raise ValueError(f'Not a directory: {directory}')
    candidates = set(directory.rglob(settings['input_pattern']))
    if ((step in ('disfilter', 'disreplay_gen') and settings['input_pattern'] == '*_disgen.root')
            or (step == 'analyse' and settings['analysis']['type'] == 'disgen'
                and settings['input_pattern'] == 'muonDis_*_disgen.root')):
        # Current gangaScript.py outputs predate the stage suffix.
        candidates.update(source for source in directory.rglob('muonDis_cudaMu_*_evt*_*.root')
                          if re.fullmatch(r'muonDis_cudaMu_.+_evt\d+_\d+\.root', source.name))
    sources = []
    for source in sorted(candidates):
        if (not source.is_file() or source.suffix != '.root'
                or '_backup' in source.stem or source.name.endswith('_rec.root')):
            continue
        source_step = next((name for name in STEPS if source.stem.endswith('_' + name)), None)
        # Custom patterns also support legacy unsuffixed inputs, but never another stage.
        expected = {'disfilter': 'disgen', 'disreplay_gen': 'disgen',
                    'disreplay_filter': 'disfilter',
                    'analyse': settings['analysis'].get('type')}.get(step)
        if source_step is not None and source_step != expected:
            continue
        if step == 'analyse':
            prefix = 'sim_' if settings['analysis']['type'].startswith('disreplay') else 'muonDis_'
            if settings['masters_per_job'] is not None:
                prefix = 'muonDis_'
            if not source.name.startswith(prefix):
                continue
            if settings['analysis']['type'].startswith('disreplay') and not settings['analysis'].get('geometry_file'):
                geometry = source.with_name('geo_' + source.name.removeprefix('sim_'))
                if not geometry.is_file():
                    if settings['masters_per_job'] is not None:
                        raise ValueError(f'Missing replay geometry: {geometry}')
                    summary['skipped_missing_geometry_inputs'] += 1
                    summary['warnings'].append(f'Missing replay geometry: {geometry}; skipping input {source}.')
                    continue
        sources.append(source)
    if not sources and not summary['skipped_missing_geometry_inputs'] and not saved:
        raise ValueError(f'No matching ROOT files in {directory} ({settings["input_pattern"]})')
    files_per_directory = {}
    for source in sources:
        files_per_directory[source.parent] = files_per_directory.get(source.parent, 0) + 1
    jobs = saved or []
    destinations = {output / plan['output_format'].format(sjid=index, fname=name)
                    for plan in jobs for index, chunk in enumerate(plan['subjobs'])
                    for name in output_names(step, chunk)}
    known_sources = {source.resolve() for plan in jobs for source in plan['sources']}
    sources = [source for source in sources if source.resolve() not in known_sources]
    generation_dirs = set()
    if step == 'analyse' and settings['masters_per_job'] is not None:
        from replayProduction import analysis_groups
        groups = analysis_groups(settings, sources, test)
    else:
        groups = [sources[offset:offset + files_per_job] for offset in range(0, len(sources), files_per_job)]
    for group in groups:
        source = group[0]
        relative = source.relative_to(directory)
        folder = relative.parent
        if step == 'disgen':
            if source.parent == directory or files_per_directory[source.parent] > 1:
                folder /= source.stem
            if folder in generation_dirs:
                raise ValueError(f'Inputs map to the same output directory: {folder}')
            generation_dirs.add(folder)
        elif output in source.parents:
            # Preserve the production tree even if input_dir selects one run/subjob.
            folder = source.relative_to(output).parent
        if step == 'analyse':
            # Group before skipping outputs/limiting jobs, keeping resume destinations stable.
            paths = [str(item.resolve()) for item in group]
            identity_text = paths[0] if len(paths) == 1 else json.dumps(paths)
            identity = hashlib.sha256(identity_text.encode()).hexdigest()[:20]
            folder = Path('analysis') / settings['analysis_name'] / identity
        split = len(ranges) > 1
        output_format = str(folder / '{sjid}' / '{fname}' if split else folder / '{fname}')
        args = ['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                '--site', settings['site'], '--', '-f', str(source), *options]
        if step == 'analyse':
            # Keep JSON quotes out of Ganga/Condor's command-line serialization.
            worker_config = dict(settings['analysis'])
            if settings['masters_per_job'] is not None:
                worker_config['require_replayfilter'] = True
            analysis_config = base64.urlsafe_b64encode(
                json.dumps(worker_config, allow_nan=False).encode('utf-8')).decode('ascii')
            source_args = [arg for item in group for arg in ('--analysis-source', str(item))]
            if settings['masters_per_job'] is not None:
                source_args = ['--analysis-sources-file', f'analysis-sources-{identity}.json']
            args = ['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                    '--site', settings['site'], *source_args,
                    '--analysis-config-b64', analysis_config,
                    '--', 'analysis-config.json']
        subjobs = []
        for index, (first, size) in enumerate(ranges):
            end = str(first + size) if size != -1 else 'all'
            if step == 'disgen':
                stem = f'muonDis_cudaMu_{settings["tag"]}_evt{first}_{end}'
            else:
                stem = production_stem(source)
                if len(ranges) > 1 or first != 0 or size != -1:
                    stem += f'_evt{first}_{end}'
            name = f'{stem}_{step}'
            if step == 'analyse':
                name = f'analysis_evt{first}_{end}'
                chunk = ['--first-entry', str(first), '--n-events', str(size), '--output-dir', name]
                names = [name + '.tar.gz']
            elif step.startswith('disreplay'):
                chunk = ['-i', str(first), '-n', str(size), '-o', '.', '--tag', name]
                names = [f'{prefix}_{name}.root' for prefix in ('sim', 'params', 'geo')]
            else:
                chunk = ['-s', str(first), '-n', str(size), '-o', name + '.root']
                names = [name + '.root']
            for filename in names:
                destination = output / output_format.format(sjid=index, fname=filename)
                if destination in destinations or destination == source:
                    raise ValueError(f'Inputs map to the same output: {destination}')
                destinations.add(destination)
            subjobs.append(chunk)
        jobs.append({'source': source, 'sources': group, 'args': args, 'subjobs': subjobs,
                     'split': split, 'output_format': output_format})
    if state is not None:
        jobs = state.set_plans(settings, jobs, output_names)
    return select_pending_jobs(settings, jobs, limit, summary, state)


def sandbox_manifest(name, data):
    manifest = SCRIPT_DIR / 'production' / '.inputs' / name
    contents = json.dumps(data, indent=2) + '\n'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with manifest.open('x') as stream:
            stream.write(contents)
    except FileExistsError:
        if manifest.read_text() != contents:
            raise ValueError(f'Sandbox manifest differs from saved inputs: {manifest}')
    return manifest


def prepare_jobs(settings, jobs, submit=False, state=None):
    outdir = str(settings['output_dir']) + '/'
    if submit:
        os.makedirs(outdir, exist_ok=True)
    config['Output']['MassStorageFile']['uploadOptions']['path'] = outdir
    config['Output']['MassStorageFile']['uploadOptions']['defaultProtocol'] = f'root://eospublic.cern.ch/{outdir}'
    wrapper = str(SCRIPT_DIR / 'wn_script_pixi.py')
    for index, plan in enumerate(jobs):
        j = Job(name=f'{settings["step"]} {settings["tag"]} - {index} - {plan["source"].name}')
        application_args = list(plan['args'])
        if not plan['split']:
            application_args.extend(plan['subjobs'][0])
        j.application = Executable(exe=File(wrapper), args=application_args)
        if settings['step'] == 'replayfilter':
            name = plan['args'][plan['args'].index('--replayfilter-config-file') + 1]
            manifest = sandbox_manifest(name, plan['filter_config'])
            j.inputsandbox = [File(str(SCRIPT_DIR / name)) for name in ('replayfilter_worker.py', 'replayChain.py')]
            j.inputsandbox.append(File(str(manifest)))
        elif settings['step'] == 'analyse' and settings['masters_per_job'] is not None:
            # Complete masters can contain thousands of files: transfer the list
            # in the sandbox instead of exceeding Condor/exec argument limits.
            name = plan['args'][plan['args'].index('--analysis-sources-file') + 1]
            manifest = sandbox_manifest(name, [str(source) for source in plan['sources']])
            j.inputsandbox = [File(str(SCRIPT_DIR / 'replayProduction.py')), File(str(manifest))]
        if plan['split']:
            j.splitter = ArgSplitter(args=plan['subjobs'], append=True)
        pattern = 'analysis_evt*.tar.gz' if settings['step'] == 'analyse' else f'*_{settings["step"]}.root'
        if settings['step'] == 'replayfilter':
            pattern = f'*_{settings["replay_type"]}_filtered*.*'
        j.outputfiles = [MassStorageFile(pattern,
                                       outputfilenameformat=plan['output_format'])]
        j.backend = Condor()
        j.backend.cdf_options['+MaxRuntime'] = str(settings['max_runtime'])
        if settings['site'] == 'CERN':
            j.backend.env['EOS_MGM_URL'] = 'root://eospublic.cern.ch'
            j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
        # The wrapper emits this only after the executable returns successfully.
        fc = FileChecker(files=['stdout'], searchStrings=['INFO: Finished running.'],
                         failIfFound=False, checkMaster=not plan['split'])
        j.postprocessors.append(fc)
        execution = f'{len(plan["subjobs"])} subjobs' if plan['split'] else 'one unsplit job'
        j.comment = f'{settings["step"]}: {execution} from {plan["source"]}'
        if 'master' in plan:
            j.comment += f'; prepared DIS file: {plan["master"]}'
        if len(plan['sources']) > 1:
            j.comment += f' and {len(plan["sources"]) - 1} more inputs'
        if 'original_range' in plan:
            j.comment += f'; repair of original range {plan["original_range"]}'
        print(f'Ganga job {j.id}: {plan["source"]} ({len(plan["sources"])} input(s), {execution})', flush=True)
        if submit:
            if state is None:
                cleanup_incomplete_outputs(plan)
                j.submit()
                continue
            state.refresh(state.registry, state.data['repository'])
            for key, expected in plan['tracking_checks'].items():
                if state.range_status(key) != expected:
                    raise ValueError('Ganga status changed since planning; rerun the preview')
            for source in plan['sources']:
                if not source.is_file():
                    raise ValueError(f'Planned input is no longer available: {source}')
            for record in plan['checks']:
                for path, expected in record.items():
                    if inspect_output(path)[0] != expected:
                        raise ValueError(f'Output changed since planning; rerun the preview: {path}')
            attempt = state.reserve(j, plan)
            try:
                cleanup_incomplete_outputs(plan)
            except BaseException:
                attempt['phase'] = 'not_submitted'
                state.save()
                raise
            attempt['phase'] = 'submitting'
            state.save()
            try:
                j.submit()
            except BaseException as error:
                # A submit exception can happen after the backend accepted work.
                attempt['phase'] = 'uncertain'
                attempt['error'] = str(error)
                state.save()
                raise
            attempt['phase'] = 'submitted'
            state.save()


def ganga_repository():
    section = config['Configuration']
    return dict(gangadir=str(Path(os.path.expandvars(section['gangadir'])).expanduser().resolve()),
                user=section['user'], type=section['repositorytype'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('yaml_file', type=Path, help='Production YAML containing one step')
    parser.add_argument('--test', action='store_true',
                        help='One unsplit job; 2 input muons for replay, 100 input entries otherwise')
    parser.add_argument('--submit', action='store_true', help='Submit prepared jobs (requires Ganga)')
    parser.add_argument('--files-per-job', type=int,
                        help='Analysis input files per job (overrides YAML; default 1)')
    parser.add_argument('--masters-per-job', type=int,
                        help='Complete replayfilter masters per analysis job (overrides YAML)')
    parser.add_argument('--n-jobs', type=int, help='Maximum new Ganga jobs in this batch (overrides YAML)')
    parser.add_argument('--state-file', type=Path, help='Persistent production plan and Ganga history')
    parser.add_argument('--retry-failed', action='store_true', default=None,
                        help='Include tracked failed/killed ranges; requires a state file')
    parser.add_argument('--status', action='store_true', help='Report tracked status without creating jobs')
    parser.add_argument('--sync-state', action='store_true',
                        help='Save current Ganga repository statuses without submitting')
    parser.add_argument('--release-job', type=int, action='append', default=[],
                        help='With --sync-state, release a missing/new tracked job after checking it is inactive')
    parser.add_argument('--adopt-job', type=int, action='append', default=[],
                        help='With --sync-state, register an existing Ganga master job (repeatable)')
    parser.add_argument('--extend-plan', action='store_true',
                        help='With --sync-state, append newly discovered inputs without regrouping saved work')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show every range command, output warning and cleanup path')
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument('--skip-existing', dest='skip_existing', action='store_true', default=None,
                          help='Skip existing outputs and fill n_jobs with new work (default)')
    existing.add_argument('--no-skip-existing', dest='skip_existing', action='store_false',
                          help='Stop on any existing output instead of skipping it')
    incomplete = parser.add_mutually_exclusive_group()
    incomplete.add_argument('--resubmit-incomplete', dest='resubmit_incomplete', action='store_true',
                            default=None, help='Delete selected incomplete output sets with --submit '
                                               'and rerun those ranges (opt-in)')
    incomplete.add_argument('--no-resubmit-incomplete', dest='resubmit_incomplete', action='store_false',
                            help='Leave empty/partial outputs untouched and continue to later work (default)')
    args = parser.parse_args()
    try:
        settings = load_settings(args.yaml_file)
        if args.files_per_job is not None:
            settings['files_per_job'] = args.files_per_job
        if args.masters_per_job is not None:
            settings['masters_per_job'] = args.masters_per_job
        if args.n_jobs is not None:
            settings['n_jobs'] = args.n_jobs
        if args.state_file is not None:
            settings['state_file'] = args.state_file.expanduser().resolve()
        if args.retry_failed is not None:
            settings['retry_failed'] = args.retry_failed
        if args.skip_existing is not None:
            settings['skip_existing'] = args.skip_existing
        if args.resubmit_incomplete is not None:
            settings['resubmit_incomplete'] = args.resubmit_incomplete
        if (args.status or args.sync_state or settings['retry_failed']) and settings['state_file'] is None:
            raise ValueError('--status, --sync-state and --retry-failed require --state-file or state_file in YAML')
        if args.release_job and not args.sync_state:
            raise ValueError('--release-job requires --sync-state')
        if args.adopt_job and not args.sync_state:
            raise ValueError('--adopt-job requires --sync-state')
        if args.extend_plan and not args.sync_state:
            raise ValueError('--extend-plan requires --sync-state')
        if sum((args.submit, args.status, args.sync_state)) > 1:
            raise ValueError('Choose only one of --submit, --status or --sync-state')
        if (args.submit or args.sync_state) and not IN_GANGA:
            raise ValueError('Run with ./gangaSubmit.sh CONFIG.yaml for submission or state synchronization')
        path = settings['state_file']
        writing = args.submit or args.sync_state
        with state_lock(path) if path is not None and writing else nullcontext():
            state = ProductionState(path, settings, args.test) if path is not None else None
            if state is not None and IN_GANGA:
                state.refresh(globals()['jobs'], ganga_repository())
            if state is not None:
                for job_id in args.release_job:
                    state.release(job_id)
            run_production(settings, args, state)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        parser.error(str(error))


def run_production(settings, args, state=None):
    summary = {}
    jobs = plan_jobs(settings, args.test, summary, state, extend=args.extend_plan)
    if args.adopt_job:
        state.adopt(globals()['jobs'], args.adopt_job, settings)
        state.refresh(globals()['jobs'], ganga_repository())
        jobs = plan_jobs(settings, args.test, summary, state)
    if state is not None and (args.submit or args.sync_state):
        state.save()
    execution = f'{summary["selected_ranges"]} executable ranges from {summary["selected_inputs"]} inputs'
    print(f'{settings["step"]}: {len(jobs)} new Ganga jobs, {execution} '
          f'({"submit" if args.submit else "prepare only; use --submit to submit"})')
    print(f'Skipped ranges: {summary["skipped_ranges"]} complete file sets, '
          f'{summary["skipped_incomplete_ranges"]} empty/partial sets left untouched.')
    if state is not None:
        print(f'State: {state.path} ({"current Ganga repository" if state.live else "saved snapshot"}).')
        print('Tracked ranges: ' + ', '.join(f'{count} {status}' for status, count in sorted(state.counts().items())))
        print(f'Skipped tracked ranges: {summary["skipped_held_ranges"]} active/unknown, '
              f'{summary["skipped_failed_ranges"]} failed/killed (--retry-failed to include).')
        if args.verbose:
            for attempt in state.data['attempts']:
                print(f'  Attempt {attempt["job_id"]}: {attempt["phase"]}, '
                      + ', '.join(f'{count} {status}' for status, count in
                                  sorted(Counter(attempt['statuses'].values()).items())))
                if attempt.get('refresh_error'):
                    print(f'    Status lookup: {attempt["refresh_error"]}')
    if summary['skipped_missing_geometry_inputs']:
        print(f'Skipped analysis inputs with missing replay geometry: '
              f'{summary["skipped_missing_geometry_inputs"]}.')
    if summary['resubmitted_ranges']:
        cleanup_files = sum(expected is not None for plan in jobs
                            for record in plan['cleanup'] for expected in record.values())
        print(f'Cleanup on submission: {cleanup_files} files across '
              f'{summary["resubmitted_ranges"]} ranges (--resubmit-incomplete enabled).')
    if args.verbose:
        for warning in summary['warnings']:
            print(f'WARNING: {warning}')
    if summary['requested_jobs'] != -1 and len(jobs) < summary['requested_jobs']:
        print(f'Only {len(jobs)} of {summary["requested_jobs"]} requested jobs are available; '
              'no more eligible work in this selection.')
    print(f'Input directory: {settings["input_dir"]}')
    print(f'Output directory: {settings["output_dir"]}')
    for index, plan in enumerate(jobs, 1):
        chunks = [chunk[chunk.index('--') + 1:] if '--' in chunk else chunk for chunk in plan['subjobs']]
        first = int(chunks[0][1])
        last_start, last_size = map(int, (chunks[-1][1], chunks[-1][3]))
        stop = 'EOF' if last_size == -1 else str(last_start + last_size)
        ranges = (f'original range {plan["original_range"]}' if 'original_range' in plan
                  else f'{len(plan["subjobs"])} range(s)')
        source = plan['source'].relative_to(settings['input_dir'])
        if len(plan['sources']) > 1:
            source = f'{source} (+{len(plan["sources"]) - 1} more inputs)'
        print(f'  {index}. {source} | {ranges}, entries [{first}, {stop}) '
              f'-> {plan["output_format"]}')
        if args.verbose:
            for record in plan['cleanup']:
                for path, expected in record.items():
                    if expected is not None:
                        print(f'  Cleanup on submission: {path}')
            for chunk in plan['subjobs']:
                print('  ' + shlex.join(['wn_script_pixi.py', *plan['args'], *chunk]))
    if not args.verbose:
        print('Use --verbose for full commands, warnings and cleanup paths.')
    if args.status or args.sync_state:
        return
    if IN_GANGA and jobs and (state is None or args.submit):
        prepare_jobs(settings, jobs, args.submit, state)
    elif jobs and state is not None:
        print('Tracked preview only; use ganga with --submit to submit this batch.')
    elif jobs:
        print('Preview only outside Ganga; use ganga to create Job objects.')


if __name__ == '__main__' or IN_GANGA:
    main()
