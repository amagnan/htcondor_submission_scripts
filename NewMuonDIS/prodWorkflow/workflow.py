#!/usr/bin/env python3
"""Plan, submit, check, retry and merge a checker-driven MuonDIS campaign."""

import argparse
from collections import Counter
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

# Ganga executes scripts in its GPI namespace, where __file__ belongs to GPI.
IN_GANGA = all(name in globals() for name in ('Job', 'Executable', 'ArgSplitter', 'config', 'jobs'))
HERE = Path(sys.argv[0] if IN_GANGA else __file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from prodWorkflow.config import load
from prodWorkflow.planning import plan, select, upstream
from prodWorkflow.state import State, state_lock
from prodWorkflow.submission import preview, submit


def parser():
    result = argparse.ArgumentParser(description=__doc__, epilog=(
        'YAML alone means plan: ./gangaSubmit.sh disgen.yaml --test --submit. '
        'Physics, resources, ranges and merge policy belong in YAML. See COMMAND --help.'))
    commands = result.add_subparsers(dest='action', required=True)
    descriptions = {
        'plan': 'Plan new work from upstream checker exports; --submit creates and submits Ganga jobs.',
        'check': 'Check the exact recorded job IDs, print stage statistics and export approved inputs.',
        'status': 'Show recorded job IDs, live Ganga statuses when available, and checker progress.',
        'retry': 'Retry only checker-rejected terminal ranges into fresh destinations; --submit executes.',
        'merge': 'Merge checked analysis archives locally in FairShip; --submit executes (no batch job).',
    }
    for action, description in descriptions.items():
        command = commands.add_parser(action, description=description, help=description)
        command.add_argument('yaml_file', type=Path, help='Step YAML (merger.yaml for merge)')
        command.add_argument('--test', action='store_true', help='Use the separate test state/output namespace; process one small master')
        command.add_argument('-v', '--verbose', action='store_true', help='Show exact task/config paths and interactive commands')
        if action in ('plan', 'retry', 'merge'):
            command.add_argument('--submit', action='store_true', help='Execute the previewed action')
        if action == 'check':
            command.add_argument('--metadata', action='store_true', help='Also print deduplicated preparation normalization totals')
            command.add_argument('--check-root', action='store_true', help='disgen only: read ROOT statistics in addition to headers')
            command.add_argument('--refresh', action='store_true', help='Revalidate unchanged successful files instead of reusing checker results')
    return result


def fairship(settings, args):
    command = ['bash', '/cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs', settings['fs_install'],
               'python3', '-u', str(HERE / 'workflow.py'), *args]
    return subprocess.run(command).returncode


def merge(state, execute=False):
    import checkAnalysis
    settings = state.settings
    checked = upstream(settings)
    results = [record['analysis'] for record in checked['records']]
    checkAnalysis.validate_analysis_sources(results)
    destination = Path(settings['merge_output'] or str(Path(settings['output_dir']) / 'merged'))
    if settings['test'] and settings['merge_output']:
        destination = destination.with_name(destination.name + '_test')
    unpack = Path(settings['unpack_dir']) if settings['unpack_dir'] else None
    if settings['test'] and unpack:
        unpack = unpack.with_name(unpack.name + '_test')
    print(f'Merge {len(results)} checked archives into {destination}')
    print(f'skip_event_records: {settings["skip_event_records"]}; save_plots: {settings["save_plots"]}')
    if not execute:
        print('Preview only; add --submit to merge locally in FairShip.')
        return 0
    args = SimpleNamespace(merge_output=destination, unpack_dir=unpack,
                           merger=Path(settings['fs_install']) / 'newMuonDIS/mergeAnalysis.py',
                           no_plots=not settings['save_plots'], skip_event_records=settings['skip_event_records'],
                           skip_tagger_tracks=False)
    checkAnalysis.merge_analysis(results, args)
    state.data['merge'] = dict(output=str(destination), upstream_state=checked['state_id'],
                              upstream_revision=checked['revision'], inputs=[r['archive'] for r in results])
    state.save()
    return 0


def main(argv=None, api=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ('plan', 'check', 'status', 'retry', 'merge', '-h', '--help'):
        argv.insert(0, 'plan')
    cli = parser()
    args = cli.parse_args(argv)
    settings = load(args.yaml_file, args.test)
    if settings['step'] == 'merger' and args.action == 'plan':
        args.action = 'merge'
    if (args.action == 'merge') != (settings['step'] == 'merger') and args.action != 'status':
        cli.error('Use merger.yaml with merge; other actions require a production step YAML')
    if args.action == 'check' and args.check_root and settings['step'] != 'disgen':
        cli.error('--check-root is only supported for disgen; other stages validate ROOT headers')
    # Checking needs ROOT; merging needs FairShip numpy/matplotlib. Ganga's Python does not.
    if api is not None and args.action in ('check', 'merge'):
        with state_lock(Path(settings['step_dir']) / 'state.json'):
            state = State(settings)
            state.refresh(api.jobs)
            if state.data['attempts']:
                state.save()
        return fairship(settings, argv)
    with state_lock(Path(settings['step_dir']) / 'state.json'):
        state = State(settings)
        if api is not None:
            state.refresh(api.jobs)
        if args.action == 'status':
            if api is not None and state.data['attempts']:
                state.save()
            print(f'{settings["name"]}: {state.path}')
            print('Statuses: ' + str(dict(Counter(state.status(unit['id']) for group in state.data['groups'] for unit in group['units']))))
            for attempt in state.data['attempts']:
                print(f'  Ganga {attempt["job_id"]}: {attempt["phase"]}; {dict(Counter(attempt["statuses"].values()))}')
                if attempt.get('error'):
                    print('    ' + attempt['error'])
            checked = state.checked()
            print(f'Checked: {len(checked["successful"])} successful, {len(checked["problems"])} pending/failed' if checked else 'Run check to create/update the next-step input list.')
            if state.data.get('merge'):
                print('Merged output: ' + state.data['merge']['output'])
            if api is None:
                print('Saved statuses only; use ./gangaSubmit.sh status FILE.yaml for live Ganga status.')
            return 0
        if args.action == 'check':
            if not state.data['groups']:
                raise ValueError('No submitted plan exists for this step/mode')
            from prodWorkflow.checking import check
            return check(state, args.metadata, args.check_root, args.refresh)
        if args.action == 'merge':
            return merge(state, args.submit)
        if args.submit and api is None:
            cli.error('Submission requires ./gangaSubmit.sh FILE.yaml --submit (or retry FILE.yaml --submit)')
        if args.action == 'retry':
            if state.checked() is None:
                raise ValueError('Run check before retry; its report identifies rejected ranges')
        else:
            state.add_groups(plan(settings))
        selected = select(state, retry=args.action == 'retry')
        preview(state, selected, args.verbose)
        if args.submit:
            state.save()
            submit(state, selected, api)
        else:
            print('Preview only; add --submit to execute.')
        return 0


if __name__ == '__main__' or IN_GANGA:
    api = SimpleNamespace(**{name: globals()[name] for name in
                            ('Job', 'Executable', 'File', 'ArgSplitter', 'MassStorageFile', 'Condor',
                             'FileChecker', 'config', 'jobs')}) if IN_GANGA else None
    try:
        code = main(api=api)
    except (OSError, ValueError, KeyError) as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        code = 1
    if IN_GANGA:
        if code:
            raise RuntimeError(f'Workflow exited with status {code}; see diagnostics above')
    else:
        sys.exit(code)
