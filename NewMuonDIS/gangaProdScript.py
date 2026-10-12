#!/usr/bin/env python3
"""Prepare one stage of a DIS production from YAML, optionally submitting with Ganga."""

import argparse
from copy import deepcopy
import os
from pathlib import Path
import re
import shlex
import sys

import yaml


# Ganga executes this file in GangaCore.GPI's namespace, not as __main__.
IN_GANGA = all(name in globals() for name in ('config', 'Job', 'Executable', 'File',
                                             'ArgSplitter', 'MassStorageFile', 'Condor', 'FileChecker'))
# In that namespace __file__ belongs to GPI; Ganga puts this script in argv[0].
SCRIPT_DIR = Path(sys.argv[0] if IN_GANGA else __file__).resolve().parent


STEPS = ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter')
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
    'n_jobs': -1,  # One job per input file; -1 selects all.
    'n_subjobs': None,
    'events_per_file': None,
    'events_per_subjob': None,
    'first_event': 0,
    'executable': None,
    'options': {},
}
REPLAY_OPTIONS = {
    '--NewMuDIS': True, '--shieldName': 'TRY_2026',
    '--field_map': 'files/2026_05_07_MainSpectrometerField_V21_3000.root',
    '--strawDesign': 10, '--noSND': True, '--debug': 0, '--seed': 0,
}
STEP_DEFAULTS = {
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
    """Return disjoint (first entry, entry count) ranges, retaining the remainder."""
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
        return [(first + i * size, size) for i in range(count)]
    if size is not None:
        needed = (total + size - 1) // size
        if count is not None and count != needed:
            raise ValueError('n_subjobs disagrees with events_per_file/events_per_subjob')
        return [(first + offset, min(size, total - offset)) for offset in range(0, total, size)]
    count = count or 1
    if count > total:
        raise ValueError('n_subjobs cannot exceed events_per_file')
    quotient, remainder = divmod(total, count)
    ranges = []
    for i in range(count):
        size = quotient + (i < remainder)
        ranges.append((first, size))
        first += size
    return ranges


def executable_options(settings):
    # These flags belong to the planner; overriding them would break splitting/naming.
    reserved = {'-f', '--inputfile', '--inputFile', '-o', '--output', '--outputfile',
                '-n', '--n_events', '--nEvents', '--tag', '-i', '--firstEvent',
                '--start_event'}
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


def plan_jobs(settings, test=False):
    integer(settings, 'max_runtime', 1)
    limit = integer(settings, 'n_jobs', 0, allow_all=True)
    ranges = event_ranges(settings, test)
    options = executable_options(settings)
    directory = settings['input_dir']
    output = settings['output_dir']
    step = settings['step']
    if not directory.is_dir():
        raise ValueError(f'Not a directory: {directory}')
    candidates = set(directory.rglob(settings['input_pattern']))
    if step in ('disfilter', 'disreplay_gen') and settings['input_pattern'] == '*_disgen.root':
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
                    'disreplay_filter': 'disfilter'}.get(step)
        if source_step is not None and source_step != expected:
            continue
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
        split = len(ranges) > 1
        output_format = str(folder / '{sjid}' / '{fname}' if split else folder / '{fname}')
        args = ['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                '--site', settings['site'], '--', '-f', str(source), *options]
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
            if step.startswith('disreplay'):
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
    if limit != -1:
        jobs = jobs[:limit]
    if test:
        jobs = jobs[:1]
    # Refuse overwriting selected outputs before creating/submitting any jobs.
    for job in jobs:
        for index, chunk in enumerate(job['subjobs']):
            if step.startswith('disreplay'):
                names = [f'{prefix}_{chunk[-1]}.root' for prefix in ('sim', 'params', 'geo')]
            else:
                names = [chunk[-1]]
            for name in names:
                destination = output / job['output_format'].format(sjid=index, fname=name)
                if os.path.lexists(destination):
                    raise ValueError(f'Output already exists: {destination}')
    return jobs


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
        j.outputfiles = [MassStorageFile(f'*_{settings["step"]}.root',
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
        print(f'Ganga job {j.id}: {plan["source"]} ({execution})', flush=True)
        if submit:
            j.submit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('yaml_file', type=Path, help='Production YAML containing one step')
    parser.add_argument('--test', action='store_true',
                        help='One unsplit job; 2 input muons for replay, 100 input entries otherwise')
    parser.add_argument('--submit', action='store_true', help='Submit prepared jobs (requires Ganga)')
    args = parser.parse_args()
    try:
        settings = load_settings(args.yaml_file)
        jobs = plan_jobs(settings, args.test)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        parser.error(str(error))
    if args.submit and not IN_GANGA:
        parser.error('Run with ./gangaSubmit.sh CONFIG.yaml --submit')
    execution = (f'{len(jobs[0]["subjobs"])} subjobs per input file'
                 if jobs and jobs[0]['split'] else 'no splitting')
    print(f'{settings["step"]}: {len(jobs)} jobs (one per input file), {execution} '
          f'({"submit" if args.submit else "prepare only; use --submit to submit"})')
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
