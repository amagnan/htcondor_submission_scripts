#!/usr/bin/env python3
"""Prepare one stage of a DIS production from YAML, optionally submitting with Ganga."""

import argparse
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


DATA_STEPS = ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter')
STEPS = (*DATA_STEPS, 'analyse')
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
    'skip_existing': True,
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
    'analyse': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip',
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
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip',
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
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip',
        'executable': 'macro/run_simScript.py',
        'input_pattern': '*_disgen.root',
        'events_per_file': -1,
        'options': REPLAY_OPTIONS,
    },
    'disreplay_filter': {
        'fs_install': '/afs/cern.ch/work/a/ammagnan/FairShip',
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
    if type(settings['skip_existing']) is not bool:
        raise ValueError('skip_existing must be true or false')
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
        settings['analysis'] = analysis
        name = settings['analysis_name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('analysis_name must be a simple directory name')
        if 'input_pattern' not in supplied:
            prefix = 'sim_' if analysis['type'].startswith('disreplay') else 'muonDis_'
            settings['input_pattern'] = prefix + '*_' + analysis['type'] + '.root'

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
    if step == 'analyse':
        return [chunk[-1] + '.tar.gz']
    if step.startswith('disreplay'):
        return [f'{prefix}_{chunk[-1]}.root' for prefix in ('sim', 'params', 'geo')]
    return [chunk[-1]]


def select_pending_jobs(settings, jobs, limit, summary):
    """Apply the new-job budget after checking each original range destination."""
    selected = []
    for job in jobs:
        if limit != -1 and len(selected) >= limit:
            break
        pending = []
        for index, chunk in enumerate(job['subjobs']):
            paths = [settings['output_dir'] / job['output_format'].format(sjid=index, fname=name)
                     for name in output_names(settings['step'], chunk)]
            # lstat distinguishes absent outputs from broken links and I/O failures.
            existing = []
            usable = []
            for path in paths:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    existing.append(False)
                    usable.append(False)
                else:
                    existing.append(True)
                    if stat.S_ISLNK(info.st_mode):
                        try:
                            info = path.stat()
                        except FileNotFoundError:
                            usable.append(False)
                            continue
                    usable.append(stat.S_ISREG(info.st_mode) and info.st_size > 0)
            if any(existing):
                if not settings['skip_existing']:
                    raise ValueError(f'Output already exists: {paths[existing.index(True)]}')
                if not all(usable):
                    raise ValueError('Incomplete output set for '
                                     f'{job["source"]}, original range {index}: '
                                     + ', '.join(str(path) for path in paths)
                                     + '. Check or move these outputs before retrying; nothing was overwritten.')
                summary['skipped_ranges'] += 1
            else:
                pending.append(index)
        if not pending:
            summary['skipped_inputs'] += 1
            continue
        if len(pending) == len(job['subjobs']):
            selected.append(job)
            continue
        # Removing ArgSplitter entries renumbers sjid. Repair each missing range
        # as an unsplit job with the original physical destination instead.
        for index in pending:
            if limit != -1 and len(selected) >= limit:
                break
            selected.append(dict(job, subjobs=[job['subjobs'][index]], split=False,
                                 output_format=job['output_format'].format(sjid=index, fname='{fname}'),
                                 original_range=index))
    summary['selected_ranges'] = sum(len(job['subjobs']) for job in selected)
    summary['selected_inputs'] = len({job['source'] for job in selected})
    summary['selected_jobs'] = len(selected)
    summary['requested_jobs'] = limit
    return selected


def plan_jobs(settings, test=False, summary=None):
    integer(settings, 'max_runtime', 1)
    limit = integer(settings, 'n_jobs', 0, allow_all=True)
    if summary is None:
        summary = {}
    summary.update(skipped_ranges=0, skipped_inputs=0)
    if test and limit != 0:
        limit = 1
    ranges = event_ranges(settings, test)
    options = executable_options(settings)
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
            if not source.name.startswith(prefix):
                continue
            if settings['analysis']['type'].startswith('disreplay') and not settings['analysis'].get('geometry_file'):
                geometry = source.with_name('geo_' + source.name[4:])
                if not geometry.is_file():
                    raise ValueError(f'Missing replay geometry: {geometry}')
        sources.append(source)
    if not sources:
        raise ValueError(f'No matching ROOT files in {directory} ({settings["input_pattern"]})')
    files_per_directory = {}
    for source in sources:
        files_per_directory[source.parent] = files_per_directory.get(source.parent, 0) + 1
    jobs = []
    destinations = set()
    generation_dirs = set()
    for source in sources:
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
            # Stable across n_jobs and nested input selections; no dependency on Ganga IDs.
            identity = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:20]
            folder = Path('analysis') / settings['analysis_name'] / identity
        split = len(ranges) > 1
        output_format = str(folder / '{sjid}' / '{fname}' if split else folder / '{fname}')
        args = ['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                '--site', settings['site'], '--', '-f', str(source), *options]
        if step == 'analyse':
            args = ['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                    '--site', settings['site'], '--analysis-source', str(source),
                    '--analysis-config', json.dumps(settings['analysis'], allow_nan=False),
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
        jobs.append({'source': source, 'args': args, 'subjobs': subjobs,
                     'split': split, 'output_format': output_format})
    return select_pending_jobs(settings, jobs, limit, summary)


def prepare_jobs(settings, jobs, submit=False):
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
        if plan['split']:
            j.splitter = ArgSplitter(args=plan['subjobs'], append=True)
        pattern = 'analysis_evt*.tar.gz' if settings['step'] == 'analyse' else f'*_{settings["step"]}.root'
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
        if 'original_range' in plan:
            j.comment += f'; repair of original range {plan["original_range"]}'
        print(f'Ganga job {j.id}: {plan["source"]} ({execution})', flush=True)
        if submit:
            j.submit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('yaml_file', type=Path, help='Production YAML containing one step')
    parser.add_argument('--test', action='store_true',
                        help='One unsplit job; 2 input muons for replay, 100 input entries otherwise')
    parser.add_argument('--submit', action='store_true', help='Submit prepared jobs (requires Ganga)')
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument('--skip-existing', dest='skip_existing', action='store_true', default=None,
                          help='Skip complete output sets and fill n_jobs with new work (default)')
    existing.add_argument('--no-skip-existing', dest='skip_existing', action='store_false',
                          help='Stop on any existing output instead of skipping it')
    args = parser.parse_args()
    try:
        settings = load_settings(args.yaml_file)
        if args.skip_existing is not None:
            settings['skip_existing'] = args.skip_existing
        summary = {}
        jobs = plan_jobs(settings, args.test, summary)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        parser.error(str(error))
    if args.submit and not IN_GANGA:
        parser.error('Run with ./gangaSubmit.sh CONFIG.yaml --submit')
    execution = f'{summary["selected_ranges"]} executable ranges from {summary["selected_inputs"]} inputs'
    print(f'{settings["step"]}: {len(jobs)} new Ganga jobs, {execution} '
          f'({"submit" if args.submit else "prepare only; use --submit to submit"})')
    print(f'Skipped while filling budget: {summary["skipped_inputs"]} complete inputs, '
          f'{summary["skipped_ranges"]} existing ranges.')
    if summary['requested_jobs'] != -1 and len(jobs) < summary['requested_jobs']:
        print(f'Only {len(jobs)} of {summary["requested_jobs"]} requested jobs are available; '
              'no more pending work in this selection.')
    print(f'Output directory: {settings["output_dir"]}')
    for plan in jobs:
        print(f'{plan["source"]} -> {settings["output_dir"]}/{plan["output_format"]}')
        for chunk in plan['subjobs']:
            print('  ' + shlex.join(['wn_script_pixi.py', *plan['args'], *chunk]))
    if IN_GANGA and jobs:
        prepare_jobs(settings, jobs, args.submit)
    elif jobs:
        print('Preview only outside Ganga; use ganga to create Job objects.')


if __name__ == '__main__' or IN_GANGA:
    main()
