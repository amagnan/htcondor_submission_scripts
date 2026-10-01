#!/usr/bin/env python3
"""Summarize filterEvents.py stdout logs from a consecutive range of Ganga jobs."""

import argparse
from collections import Counter
from pathlib import Path
import math
import re
import shlex
import statistics

from checkPrepareEventsOutputs import output_locations, root_statistics


NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
WDIS_SCALE = 0.006 * 1e-27 * 6.02214076e23


def parse_stdout(text):
    """Return (status, counts); only complete, zero-exit jobs have counts."""
    exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
    if not exits:
        return 'incomplete (no exit code)', None
    if int(exits[-1]) != 0:
        return 'failed (exit code ' + exits[-1] + ')', None
    saved = re.findall(r'MuDISFilter: saved (\d+) muon entries; skipped (\d+)', text)
    sizes = [int(n) for n in re.findall(r'\bReading (\d+) entries\.', text)]
    commands = re.findall(r'^INFO: Executing: (.+)$', text, re.MULTILINE)
    materials = re.findall(
        rf'MuDISFilter: selected DIS events in (\S+): raw = (\d+), weighted = ({NUMBER})', text)
    if len(saved) != 1 or not sizes or len(commands) != 1 or not materials:
        return 'incomplete (missing or ambiguous filter summary)', None
    try:
        tokens = shlex.split(commands[0])
        script_index = next(i for i, token in enumerate(tokens)
                            if Path(token).name == 'filterEvents.py')
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument('-n', '--n_events', type=int, default=-1)
        parser.add_argument('-s', '--start_event', type=int, default=0)
        parser.add_argument('--filter-option', type=int, choices=(0, 1, 2))
        parser.add_argument('-o', '--outputfile')
        args, _ = parser.parse_known_args(tokens[script_index + 1:])
    except (ValueError, StopIteration, SystemExit):
        return 'incomplete (cannot parse filter arguments)', None
    if args.n_events < -1 or args.start_event < 0:
        return 'incomplete (invalid event range)', None
    if args.filter_option is None:
        # Older productions used *_filtered.root without --filter-option.
        # Also recognize the newer *_filtered_0.root (and _1/_2) names.
        suffix = re.search(r'_filtered(?:_([012]))?\.root$', args.outputfile or '')
        args.filter_option = int(suffix.group(1) or 0) if suffix else 0
    # MuDISFilter applies the requested range separately to each input tree.
    processed = sum(max(0, size - args.start_event) if args.n_events == -1
                    else max(0, min(size - args.start_event, args.n_events)) for size in sizes)
    selected, skipped = map(int, saved[0])
    material_counts = {name: (int(raw), float(weighted) * WDIS_SCALE)
                       for name, raw, weighted in materials}
    if (len(material_counts) != len(materials) or selected + skipped > processed
            or any(not math.isfinite(weighted) for _, weighted in material_counts.values())):
        return 'incomplete (inconsistent counts)', None
    if 'Performance (initialization + filtering):' not in text:
        return 'incomplete (missing filter completion)', None
    return 'successful', {'processed': processed, 'selected': selected,
                          'skipped': skipped, 'materials': material_counts,
                          'filter_option': args.filter_option}


def print_stats(label, values):
    total = sum(values)
    mean = statistics.mean(values)
    deviation = statistics.pstdev(values)
    print(f'{label:<30} {total:>15.8g} {mean:>15.8g} {deviation:>15.8g}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Example: python3 checkFilterOutputs.py ~/gangadir/workspace/ammagnan/LocalXML '
        '--first-job 500 --num-jobs 700. Reads PATH/JOB/output/stdout without modifying files.'))
    parser.add_argument('path', type=Path, help='Directory containing numbered Ganga job directories')
    parser.add_argument('--first-job', type=int, required=True)
    parser.add_argument('--num-jobs', type=int, required=True)
    parser.add_argument('--filter-option', type=int, choices=(0, 1, 2),
                        help='Summarize only this filter option (default: all options); '
                             'option 0 includes legacy *_filtered.root and *_filtered_0.root logs')
    args = parser.parse_args()
    if args.first_job < 0 or args.num_jobs <= 0:
        parser.error('--first-job must be nonnegative and --num-jobs must be positive')
    base = args.path.expanduser()
    if not base.is_dir():
        parser.error(f'Not a directory: {base}')
    statuses = Counter()
    problems = []
    results = []
    excluded = []
    weighted_results = []
    weight_problems = []
    for job_id in range(args.first_job, args.first_job + args.num_jobs):
        logfile = base / str(job_id) / 'output/stdout'
        try:
            log_text = logfile.read_text(errors='replace')
            status, result = parse_stdout(log_text)
        except FileNotFoundError:
            status, result = 'missing stdout', None
        except OSError as error:
            status, result = 'unreadable stdout', None
            problems.append(f'{job_id}: {error}')
        if (result is not None and args.filter_option is not None
                and result['filter_option'] != args.filter_option):
            excluded.append(f"{job_id}: filter option {result['filter_option']}")
            continue
        statuses[status] += 1
        if result is not None:
            results.append(result)
            destinations = re.findall(r'^Output copy completed: (.+)$', log_text, re.MULTILINE)
            try:
                if not destinations:
                    destinations = output_locations(base / str(job_id))
                if len(destinations) != 1:
                    weight_problems.append(f'{job_id}: missing or ambiguous ROOT output path')
                    continue
                weight_status, weights = root_statistics(destinations[0], weights_only=True)
                if weights is None:
                    weight_problems.append(f'{job_id}: {weight_status}')
                elif weights['written'] != result['selected']:
                    weight_problems.append(f'{job_id}: ROOT entries differ from selected log count')
                else:
                    weighted_results.append(weights['weighted_muons'])
            except OSError as error:
                weight_problems.append(f'{job_id}: cannot read output locations ({error})')
        else:
            problems.append(f'{job_id}: {status}')
    print(f'Jobs {args.first_job}–{args.first_job + args.num_jobs - 1}')
    if args.filter_option is not None:
        print(f'Filter option: {args.filter_option}; excluded other-option jobs: {len(excluded)}')
    print(f'Successful jobs with complete summaries: {len(results)}/{args.num_jobs} '
          f'({100 * len(results) / args.num_jobs:.2f}%)')
    for status, count in sorted(statuses.items()):
        if status != 'successful':
            print(f'  {status}: {count}')
    if results:
        print('\nStatistics include only successful jobs with complete summaries.')
        print(f'All weighted DIS counts scaled by {WDIS_SCALE:.8g} (0.006 * 1e-27 × 6.02214076e23).')
        print('SD = population standard deviation of the per-job counts about their mean.')
        print(f'{"Quantity":<30} {"Sum":>15} {"Mean/job":>15} {"SD/job":>15}')
        for key in ('processed', 'selected', 'skipped'):
            print_stats('Muon entries ' + key, [result[key] for result in results])
        print('Weighted muons = sum(muon_MCTracks.fW[0]); no DIS scaling applied.')
        print(f'Muon weights available for {len(weighted_results)}/{len(results)} successful jobs.')
        if weighted_results:
            print_stats('Muons selected weighted', weighted_results)
        if weight_problems:
            print('WARNING: weighted-muon statistics use only jobs with readable, matching ROOT output.')
        processed = sum(result['processed'] for result in results)
        selected = sum(result['selected'] for result in results)
        print(f'Muon selection: {selected}/{processed}'
              + (f' ({100 * selected / processed:.4f}%)' if processed else ' (undefined fraction)'))
        materials = sorted(set().union(*(result['materials'] for result in results)))
        for material in materials:
            values = [result['materials'][material] for result in results if material in result['materials']]
            if len(values) != len(results):
                print(f'WARNING: {material} reported by {len(values)}/{len(results)} jobs; '
                      'its statistics use reporting jobs only.')
            print_stats('DIS ' + material + ' raw', [value[0] for value in values])
            print_stats('DIS ' + material + ' weighted', [value[1] for value in values])
        for index, label in enumerate(('raw', 'weighted')):
            print_stats('DIS ALL materials ' + label,
                        [sum(value[index] for value in result['materials'].values()) for result in results])
    if problems:
        print('\nJobs requiring attention:')
        print('\n'.join(problems))
    if excluded:
        print('\nExcluded jobs (different filter option):')
        print('\n'.join(excluded))
    if weight_problems:
        print('\nJobs with unavailable muon weights:')
        print('\n'.join(weight_problems))


if __name__ == '__main__':
    main()
