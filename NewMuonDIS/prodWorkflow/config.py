"""Small, strict YAML interface; physics settings stay out of the CLI."""

from copy import deepcopy
import getpass
from pathlib import Path

import yaml


STEPS = ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter',
         'replayfilter', 'analysis', 'merger')
PARENTS = {'disfilter': ('disgen',), 'disreplay_gen': ('disgen',),
           'disreplay_filter': ('disfilter',),
           'replayfilter': ('disreplay_gen', 'disreplay_filter'),
           'analysis': ('replayfilter',), 'merger': ('analysis',)}
COMMON = dict(production='new_production', tag='TRY2026', state_dir='.state',
              output_base='/eos/experiment/ship/user/ammagnan/MuonDIS',
              fs_install='/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs',
              workspace=f'~/gangadir/workspace/{getpass.getuser()}/LocalXML',
              max_runtime=86000, memory_mb=3000, disk_mb=50000, cpus=1,
              accounting_group='group_u_SHIP.u_ship_cg')
DEFAULTS = dict(name=None, upstream=None, require_complete=True, n_jobs=-1,
                first_event=0, events_per_file=-1, events_per_subjob=None,
                n_subjobs=None, input_dir=None, input_pattern='*sim_*.root',
                geometry_file=None, options={}, analysis={},
                skip_event_records=True, save_plots=True, merge_output=None,
                unpack_dir=None)
EXECUTABLES = {'disgen': 'newMuonDIS/prepareEvents.py',
               'disfilter': 'newMuonDIS/filterEvents.py',
               'disreplay_gen': 'macro/run_simScript.py',
               'disreplay_filter': 'macro/run_simScript.py',
               'analysis': 'newMuonDIS/analyseEvents.py'}
OPTIONS = {
    'disgen': {'--nDIS': 100, '--z_min': 2000, '--z_max': 9500, '--debug': 0},
    'disfilter': {'--filter-option': 1, '--pythia-decays': True},
    'disreplay_gen': {'--NewMuDIS': True, '--shieldName': 'TRY_2026',
                      '--field_map': 'files/2026_07_02_MainSpectrometerField_V21_2455.root',
                      '--strawDesign': 10, '--noSND': True, '--debug': 0, '--seed': 0},
}
OPTIONS['disreplay_filter'] = OPTIONS['disreplay_gen']
ANALYSIS = dict(save_plots=False, weight_factor=3.6e-6, target_sum_weights=None,
                failed_job_factor=1.0, geometry_file=None,
                plot_axes=dict(x=[-500, 500, 500], y=[-500, 500, 500], z=[2000, 10000, 800]))


def mapping(path):
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f'Expected a YAML mapping: {path}')
    return data


def path_value(value, base):
    path = Path(value).expanduser()
    return str((base / path).resolve() if not path.is_absolute() else path.resolve())


def load(path, test=False):
    path = Path(path).expanduser().resolve()
    supplied = mapping(path)
    unknown = supplied.keys() - (COMMON.keys() | DEFAULTS.keys() | {'workflow', 'step'})
    if unknown:
        raise ValueError(f'Unknown settings in {path.name}: {sorted(unknown)}')
    common_path = path.parent / supplied.get('workflow', 'workflow.yaml')
    common = mapping(common_path)
    if common.keys() - COMMON.keys():
        raise ValueError(f'Unknown common settings: {sorted(common.keys() - COMMON.keys())}')
    settings = {**deepcopy(COMMON), **common, **deepcopy(DEFAULTS), **supplied}
    step = settings.get('step')
    if step not in STEPS:
        raise ValueError(f'step must be one of {", ".join(STEPS)}')
    only = {'input_pattern': ('disgen',), 'geometry_file': ('replayfilter',),
            'skip_event_records': ('merger',), 'save_plots': ('merger',),
            'merge_output': ('merger',), 'unpack_dir': ('merger',)}
    for key, steps in only.items():
        if key in supplied and step not in steps:
            raise ValueError(f'{key} applies only to {", ".join(steps)}')
    if step == 'merger' and any(key in supplied for key in ('first_event', 'events_per_file', 'events_per_subjob', 'n_subjobs')):
        raise ValueError('merger consumes checked archives; it does not split event ranges')
    settings['name'] = settings['name'] or step
    settings['upstream'] = settings['upstream'] or (PARENTS[step][0] if step in PARENTS else None)
    for key in ('name', 'production', 'tag', 'upstream'):
        value = settings[key]
        if value is None and key == 'upstream':
            continue
        value = str(value)
        if not value or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
            raise ValueError(f'{key} must contain only letters, digits, underscores or hyphens')
        settings[key] = value
    if settings['name'] == settings['upstream']:
        raise ValueError('A step cannot be its own upstream')
    for key in ('max_runtime', 'memory_mb', 'disk_mb', 'cpus'):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('n_jobs', 'events_per_file'):
        if type(settings[key]) is not int or settings[key] not in (-1,) and settings[key] <= 0:
            raise ValueError(f'{key} must be positive or -1')
    if type(settings['first_event']) is not int or settings['first_event'] < 0:
        raise ValueError('first_event must be a nonnegative integer')
    for key in ('n_subjobs', 'events_per_subjob'):
        if settings[key] is not None and (type(settings[key]) is not int or settings[key] < 1):
            raise ValueError(f'{key} must be null or a positive integer')
    if settings['n_subjobs'] is not None and settings['events_per_subjob'] is not None:
        raise ValueError('Choose n_subjobs or events_per_subjob')
    for key in ('require_complete', 'skip_event_records', 'save_plots'):
        if type(settings[key]) is not bool:
            raise ValueError(f'{key} must be true or false')
    for key in ('state_dir', 'output_base', 'fs_install', 'workspace'):
        base = path.parent if key in supplied else common_path.parent
        settings[key] = path_value(settings[key], base)
    for key in ('input_dir', 'geometry_file', 'merge_output', 'unpack_dir'):
        if settings[key] is not None:
            settings[key] = path_value(settings[key], path.parent)
    if step == 'disgen' and (not settings['input_dir'] or settings['events_per_file'] == -1):
        raise ValueError('disgen requires input_dir and a finite events_per_file')
    if step != 'disgen' and settings['input_dir'] is not None:
        raise ValueError('Only disgen takes input_dir; later steps use the upstream checked.json')
    if not isinstance(settings['options'], dict) or not isinstance(settings['analysis'], dict):
        raise ValueError('options and analysis must be mappings')
    if step not in OPTIONS and settings['options']:
        raise ValueError(f'{step} does not take executable options')
    settings['options'] = {**deepcopy(OPTIONS.get(step, {})), **settings['options']}
    if step == 'analysis':
        if settings['analysis'].keys() - ANALYSIS.keys():
            raise ValueError('Unknown analysis setting; type, input, range and output are managed by the workflow')
        settings['analysis'] = {**deepcopy(ANALYSIS), **settings['analysis']}
        if settings['analysis']['geometry_file']:
            settings['analysis']['geometry_file'] = path_value(settings['analysis']['geometry_file'], path.parent)
    elif settings['analysis']:
        raise ValueError('The analysis mapping is only used by step: analysis')
    settings['test'] = test
    settings['config_file'] = str(path)
    mode = 'test' if test else 'production'
    settings['campaign_dir'] = str(Path(settings['state_dir']) / settings['tag'] / settings['production'] / mode)
    settings['step_dir'] = str(Path(settings['campaign_dir']) / settings['name'])
    settings['output_dir'] = str(Path(settings['output_base']) / settings['tag'] / settings['production'] / mode / settings['name'])
    return settings


def signature(settings):
    # Runtime resources and batch size can change without changing physics or inputs.
    excluded = {'config_file', 'workflow', 'n_jobs', 'require_complete',
                'max_runtime', 'memory_mb', 'disk_mb', 'cpus', 'accounting_group'}
    return {key: value for key, value in settings.items() if key not in excluded}
