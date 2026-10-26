"""Ganga boundary: reserve job IDs before submitting and never overwrite attempts."""

import os
from pathlib import Path
import shlex
import uuid

from prodWorkflow.state import verify, write_json


ROOT = Path(__file__).resolve().parents[1]


def preview(state, groups, verbose=False):
    settings = state.settings
    print(f'{settings["name"]}: {len(groups)} Ganga masters, '
          f'{sum(len(group["units"]) for group in groups)} executable ranges '
          f'({"test" if settings["test"] else "production"})')
    print(f'State: {state.path}')
    for group in groups:
        print(f'  {group["id"]}: {len(group["inputs"]["sources"])} inputs, {len(group["units"])} ranges')
        if verbose:
            for unit in group['units']:
                spec = unit['spec']
                path = Path(settings['step_dir']) / 'preview' / f'task-{unit["id"]}.json'
                write_json(path, spec)
                print(f'    Entries [{spec["first"]}, {spec["first"] + spec["count"]}); config: {path}')
                print('    Interactive (fresh directory each time):')
                print('    (cd "$(mktemp -d /tmp/muondis-debug.XXXXXX)" && ' +
                      shlex.join(['python3', '-u', str(ROOT / 'prodWorkflow/worker.py'), str(path)]) + ')')
    if not verbose:
        print('Use --verbose for readable task files and interactive commands.')


def submit(state, groups, api):
    settings = state.settings
    section = api.config['Configuration']
    actual_workspace = Path(os.path.expandvars(section['gangadir'])).expanduser() / 'workspace' / section['user'] / section['repositorytype']
    if actual_workspace.resolve() != Path(settings['workspace']):
        raise ValueError(f'YAML workspace differs from the current Ganga repository: {actual_workspace}')
    outdir = settings['output_dir'] + '/'
    storage = api.config['Output']['MassStorageFile']['uploadOptions']
    storage['path'] = outdir
    storage['defaultProtocol'] = f'root://eospublic.cern.ch/{outdir}'
    Path(outdir).mkdir(parents=True, exist_ok=True)
    for group in groups:
        verify(group['inputs']['files'])
        attempt_id = uuid.uuid4().hex
        folder = Path(settings['step_dir']) / 'tasks' / attempt_id
        task_paths = []
        output_map = {}
        for index, unit in enumerate(group['units']):
            path = folder / f'task-{unit["id"]}.json'
            write_json(path, unit['spec'])
            task_paths.append(path)
            output_map[unit['id']] = [str(Path(outdir) / group['id'] / attempt_id / str(index) / name)
                                      for name in unit['spec']['outputs']]
        split = len(task_paths) > 1
        job = api.Job(name=f'{settings["name"]} {settings["production"]} {group["id"]}')
        job.application = api.Executable(exe=api.File(str(ROOT / 'prodWorkflow/worker.py')),
                                         args=[] if split else [task_paths[0].name])
        if split:
            job.splitter = api.ArgSplitter(args=[[path.name] for path in task_paths], append=True)
        job.inputfiles = [str(path) for path in task_paths]
        job.inputfiles.extend(str(ROOT / name) for name in
                              ('wn_script_pixi.py', 'replayfilter_worker.py', 'replayChain.py'))
        # Each subjob produces its own range's files; do not require siblings' outputs.
        step = settings['step']
        pattern = ('analysis_evt*.tar.gz' if step == 'analysis' else
                   '*_filtered.*' if step == 'replayfilter' else f'*_{step}.root')
        output_format = f'{group["id"]}/{attempt_id}/' + ('{sjid}' if split else '0') + '/{fname}'
        job.outputfiles = [api.MassStorageFile(pattern, outputfilenameformat=output_format)]
        job.backend = api.Condor()
        job.backend.cdf_options.update({'+MaxRuntime': str(settings['max_runtime']),
                                        'request_memory': str(settings['memory_mb']),
                                        'request_disk': f'{settings["disk_mb"]}MB',
                                        'request_cpus': str(settings['cpus'])})
        if settings['accounting_group']:
            job.backend.cdf_options['accounting_group'] = settings['accounting_group']
        job.backend.env['EOS_MGM_URL'] = 'root://eospublic.cern.ch'
        job.backend.env['PYTHONUNBUFFERED'] = '1'
        job.postprocessors.append(api.FileChecker(files=['stdout'], searchStrings=['INFO: Finished running.'],
                                                   failIfFound=False, checkMaster=not split))
        attempt = dict(id=attempt_id, job_id=job.id, group=group['id'],
                       units={unit['id']: index if split else None for index, unit in enumerate(group['units'])},
                       statuses={unit['id']: 'submitting' for unit in group['units']}, outputs=output_map,
                       phase='reserved')
        job.comment = state.marker(attempt)
        state.data['attempts'].append(attempt)
        state.data['revision'] += 1
        state.save()
        print(f'Ganga job {job.id}: {settings["name"]}, {len(group["units"])} ranges; attempt {attempt_id}', flush=True)
        try:
            job.submit()
        except BaseException as error:
            attempt['phase'] = 'uncertain'
            attempt['error'] = str(error)
            state.save()
            raise
        attempt['phase'] = 'submitted'
        state.save()
