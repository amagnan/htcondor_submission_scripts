#!/usr/bin/env python3
"""Plan, submit, check, retry, rerun and merge a checker-driven MuonDIS campaign."""

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
from prodWorkflow.submission import monitor, preview, submit


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
        'rerun': 'Reset only this step\'s state/checker results and plan again; --submit executes.',
        'merge': 'Merge checked analysis archives locally in FairShip; --submit executes (no batch job).',
        'merge-diagnostics': 'Merge deferred replayfilter diagnostics using the saved baseline plan; --submit executes.',
    }
    for action, description in descriptions.items():
        command = commands.add_parser(action, description=description, help=description)
        command.add_argument('yaml_file', type=Path, help='Step YAML (merger.yaml for merge)')
        command.add_argument('--test', action='store_true', help='Use the separate test state/output namespace; process one small master')
        command.add_argument('-v', '--verbose', action='store_true', help='Show exact task/config paths and interactive commands')
        if action in ('plan', 'retry', 'rerun', 'merge', 'merge-diagnostics'):
            command.add_argument('--submit', action='store_true', help='Execute the previewed action')
        if action == 'plan':
            command.add_argument('--rerun', action='store_true',
                                 help='Rerun this production step using checked upstream inputs; same as the rerun command')
        if action in ('plan', 'merge'):
            command.add_argument('--writeToEOS', action='store_true',
                                 help='For merger YAML only: use output_base (EOS) instead of local results; explicit YAML paths override this')
        if action == 'merge-diagnostics':
            command.add_argument('--split', action='store_true',
                                 help='Publish histograms and smaller tree parts separately; release each local part after publication')
            command.add_argument('--tree-part-gib', type=int, default=4,
                                 help='With --split: maximum combined input ROOT GiB per tree part (default: 4; output size can differ)')
        if action == 'check':
            command.add_argument('--metadata', action='store_true', help='Also print deduplicated preparation normalization totals')
            command.add_argument('--check-root', action='store_true', help='Read disgen statistics or replay/analysis scalar path lengths in addition to headers')
            command.add_argument('--refresh', action='store_true', help='Revalidate unchanged successful files instead of reusing checker results')
    return result


def fairship(settings, args):
    command = ['bash', '/cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs', settings['fs_install'],
               'python3', '-u', str(HERE / 'workflow.py'), *args]
    return subprocess.run(command).returncode


def merge(state, execute=False, write_to_eos=False):
    from prodWorkflow import checkAnalysis, replayMerge
    settings = state.settings
    checked = upstream(settings)
    results = [record['analysis'] for record in checked['records']]
    checkAnalysis.validate_analysis_sources(results)
    if any(result['manifest']['output_version'] not in (7, 8) for result in results):
        raise ValueError('Workflow merging requires version-7/8 ROOT outputs and matching FairShip code; '
                         'rerun analysis in a new step/campaign')
    tree_only = results[0]['manifest'].get('tree_only', False)
    if tree_only and settings['skip_event_records']:
        raise ValueError('Cannot omit event records from a tree-only merge; set skip_event_records: false')
    mode = 'test' if settings['test'] else 'production'
    local_dir = (Path(settings['config_file']).parent / 'results' / settings['tag'] /
                 settings['production'] / mode / settings['name'])
    replay_plan = replayMerge.plan_merge(results, test=settings['test'],
                                        defer_diagnostics=settings.get('defer_diagnostics', False)) if settings.get('merge_replayfilter', False) else None
    replay_local = local_dir / 'replayfilter_merged'
    if write_to_eos:
        output_dir = Path(settings['output_dir'])
    else:
        output_dir = local_dir
    destination = Path(settings['merge_output']) if settings['merge_output'] else output_dir / 'merged'
    if settings['test'] and settings['merge_output']:
        destination = destination.with_name(destination.name + '_test')
    unpack = Path(settings['unpack_dir']) if settings['unpack_dir'] else None
    if settings['test'] and unpack:
        unpack = unpack.with_name(unpack.name + '_test')
    summary = destination.with_name(destination.name + '_summary') if tree_only and settings['summarize'] else None
    summarizer = Path(settings['fs_install']) / 'newMuonDIS/summarizeAnalysis.py'
    if summary:
        if not summarizer.is_file():
            raise ValueError(f'Summarizer not found: {summarizer}')
        if summary.exists() and (not summary.is_dir() or any(summary.iterdir())):
            raise ValueError(f'Summary output must be new or empty: {summary}')
        input_dir = (unpack or destination.with_name(destination.name + '_inputs')).resolve()
        summary_dir = summary.resolve()
        if summary_dir == input_dir or summary_dir in input_dir.parents or input_dir in summary_dir.parents:
            raise ValueError('Summary output and unpack directory must be separate')
    print(f'Merge {len(results)} checked archives into {destination}')
    print(f'skip_event_records: {settings["skip_event_records"]}; save_plots: {settings["save_plots"]}')
    if summary:
        print(f'Summarize merged event tree into {summary}')
    if replay_plan:
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError(f'Merge output must be new or empty: {destination}')
        publish_dir = Path(replay_plan['destination'])
        for path in (destination, unpack or destination.with_name(destination.name + '_inputs'), summary):
            if path is not None:
                path = Path(path).resolve()
                if any(path == other or path in other.parents or other in path.parents
                       for other in (publish_dir, replay_local)):
                    raise ValueError('Analysis and replayfilter merge destinations must be separate')
        print(f'Merge {len(replay_plan["inputs"])} whole replayfilter files ({replay_plan["entries"]} entries) into {replay_local}')
        print(f'Publish ROOT, geometry and manifest to {publish_dir} (independent of --writeToEOS)')
        if replay_plan.get('defer_diagnostics'):
            print('Replayfilter diagnostics deferred; run merge-diagnostics with this YAML after the baseline merge.')
    if not execute:
        print('Preview only; add --submit to merge locally in FairShip.')
        return 0
    if replay_plan:
        replayMerge.execute(state, replay_plan, replay_local)
    args = SimpleNamespace(merge_output=destination, unpack_dir=unpack,
                           merger=Path(settings['fs_install']) / 'newMuonDIS/mergeAnalysis.py',
                           no_plots=not settings['save_plots'], skip_event_records=settings['skip_event_records'],
                           skip_tagger_tracks=False)
    checkAnalysis.merge_analysis(results, args)
    state.data['merge'] = dict(output=str(destination), upstream_state=checked['state_id'],
                              upstream_revision=checked['revision'], inputs=[r['archive'] for r in results])
    state.save()
    if summary:
        command = [sys.executable, str(summarizer), str(destination), '-o', str(summary)]
        if not settings['save_plots']:
            command.append('--no-plots')
        subprocess.run(command, check=True)
        state.data['merge']['summary'] = str(summary)
        state.save()
    return 0


def merge_diagnostics(state, execute=False, split=False, tree_part_gib=4):
    from prodWorkflow import replayMerge
    baseline = state.data.get('replayfilter_merge')
    if not baseline or baseline['phase'] != 'published':
        raise ValueError('Complete the baseline replayfilter merge before merging deferred diagnostics')
    if split:
        plans = replayMerge.plan_diagnostic_parts(baseline['plan'], tree_part_gib * 1024 ** 3)
        local = Path(baseline['local']).with_name('replayfilter_diagnostics_parts')
        print(f'Split diagnostics: histograms then {len(plans) - 1} tree parts, at most {tree_part_gib} input GiB each')
        print(f'Build each part under {local}; publish under {baseline["plan"]["destination"]}_diagnostics/')
        print('Release each local part only after verified publication; published parts are reused on retry.')
        saved = state.data.get('replayfilter_diagnostics_parts')
        identities = [plan['id'] for plan in plans]
        if saved and saved['plans'] != identities:
            raise ValueError('Split diagnostic plan changed; keep the original --tree-part-gib')
        if not execute:
            print('Preview only; add --submit to merge split diagnostics.')
            return 0
        replayMerge.published_manifest(baseline['plan']['destination'], baseline['plan'])
        state.data['replayfilter_diagnostics_parts'] = dict(plans=identities, phase='building')
        state.save()
        for plan in plans:
            replayMerge.execute(state, plan, local / plan['diagnostic_part'], release_local=True)
        state.data['replayfilter_diagnostics_parts']['phase'] = 'published'
        state.save()
        return 0
    plan = replayMerge.plan_diagnostics(baseline['plan'])
    local = Path(baseline['local']).with_name('replayfilter_diagnostics')
    print(f'Merge deferred diagnostics for {len(plan["inputs"])} inputs into {local}')
    print(f'Publish diagnostics to {plan["destination"]}; baseline plan: {plan["baseline_plan_id"]}')
    if not execute:
        print('Preview only; add --submit to merge diagnostics locally in FairShip.')
        return 0
    # Use the completed baseline plan, even if upstream checker exports have changed.
    replayMerge.published_manifest(baseline['plan']['destination'], baseline['plan'])
    replayMerge.execute(state, plan, local)
    return 0


def main(argv=None, api=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ('plan', 'check', 'status', 'retry', 'rerun', 'merge', 'merge-diagnostics', '-h', '--help'):
        argv.insert(0, 'plan')
    cli = parser()
    args = cli.parse_args(argv)
    settings = load(args.yaml_file, args.test)
    if getattr(args, 'rerun', False):
        if settings['step'] == 'merger':
            cli.error('--rerun requires a production step YAML; use merge for merger YAML')
        args.action = 'rerun'
    if settings['step'] == 'merger' and args.action == 'plan':
        args.action = 'merge'
    if getattr(args, 'writeToEOS', False) and args.action != 'merge':
        cli.error('--writeToEOS is only supported for merging with merger YAML')
    if (args.action in ('merge', 'merge-diagnostics')) != (settings['step'] == 'merger') and args.action != 'status':
        cli.error('Use merger.yaml with merge; other actions require a production step YAML')
    if args.action == 'check' and args.check_root and settings['step'] not in (
            'disgen', 'disreplay_gen', 'disreplay_filter', 'replayfilter', 'analysis'):
        cli.error('--check-root is supported for disgen, disreplay, replayfilter and replay analysis')
    # Checking needs ROOT; merging needs FairShip numpy/matplotlib. Ganga's Python does not.
    if api is not None and args.action in ('check', 'merge', 'merge-diagnostics'):
        with state_lock(Path(settings['step_dir']) / 'state.json'):
            state = State(settings, inspect_existing=args.action == 'check')
            if state.prune_unsubmitted_groups():
                state.save()
            state.refresh(api.jobs, getattr(api, 'monitor', None))
            if state.data['attempts']:
                state.save()
        return fairship(settings, argv)
    with state_lock(Path(settings['step_dir']) / 'state.json'):
        state = State(settings, fresh=args.action == 'rerun', inspect_existing=args.action in ('check', 'status'))
        if state.prune_unsubmitted_groups():
            state.save()
        if api is not None:
            state.refresh(api.jobs, getattr(api, 'monitor', None))
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
            if state.data.get('replayfilter_merge'):
                merged = state.data['replayfilter_merge']
                print(f'Replayfilter merge: {merged["phase"]}; destination: {merged["plan"]["destination"]}')
            if state.data.get('replayfilter_diagnostics_parts'):
                parts = state.data['replayfilter_diagnostics_parts']
                print(f'Split replayfilter diagnostics: {parts["phase"]}; {len(parts["plans"])} parts')
            elif state.data.get('replayfilter_diagnostics'):
                merged = state.data['replayfilter_diagnostics']
                print(f'Replayfilter diagnostics: {merged["phase"]}; destination: {merged["plan"]["destination"]}')
            if api is None:
                print('Saved statuses only; use ./gangaSubmit.sh status FILE.yaml for live Ganga status.')
            return 0
        if args.action == 'check':
            if not state.data['groups']:
                raise ValueError('No submitted plan exists for this step/mode')
            from prodWorkflow.checking import check
            return check(state, args.metadata, args.check_root, args.refresh)
        if args.action == 'merge':
            return merge(state, args.submit, write_to_eos=args.writeToEOS)
        if args.action == 'merge-diagnostics':
            return merge_diagnostics(state, args.submit, split=args.split, tree_part_gib=args.tree_part_gib)
        if args.submit and api is None:
            cli.error('Submission requires ./gangaSubmit.sh FILE.yaml --submit (or retry FILE.yaml --submit)')
        if args.action == 'retry':
            if state.checked() is None:
                raise ValueError('Run check before retry; its report identifies rejected ranges')
        else:
            state.add_groups(plan(settings, state))
        selected = select(state, retry=args.action == 'retry')
        if args.action == 'rerun':
            print(f'Rerun resets only {state.path} and {state.path.with_name("checked.json")}.')
            print('Existing outputs and Ganga jobs are retained; upstream and downstream state are unchanged.')
        preview(state, selected, args.verbose)
        if args.submit:
            state.save()
            if args.action == 'rerun':
                state.path.with_name('checked.json').unlink(missing_ok=True)
            submit(state, selected, api)
        else:
            print('Preview only; add --submit to execute.')
        return 0


if __name__ == '__main__' or IN_GANGA:
    api = SimpleNamespace(**{name: globals()[name] for name in
                            ('Job', 'Executable', 'File', 'ArgSplitter', 'MassStorageFile', 'Condor',
                             'FileChecker', 'config', 'jobs')}) if IN_GANGA else None
    if api is not None:
        api.monitor = monitor
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
