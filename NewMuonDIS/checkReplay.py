#!/usr/bin/env python3
"""Check Ganga NewMuDIS replay logs and summarize events, restored hits and timing.

Example: python3 checkReplay.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 3225 --num-jobs 1

Reads JOB_ID/SUBJOB_ID/output/stdout. Only successful subjobs with complete
summaries contribute to statistics. ROOT files and output transfers are not
validated, and no files are modified.
"""

import argparse
import ast
from collections import Counter
from pathlib import Path
import math
import os
import re
import sys

from checkPrepareEventsOutputs import NUMBER, print_stats, subjob_logs
from jobTiming import print_job_timing, read_job_timing


HIT_COLLECTIONS = ('UpstreamTaggerPoint', 'vetoPoint', 'strawtubesPoint', 'TimeDetPoint')


def parse_stdout(text):
    """Return replay statistics only for a complete, zero-exit worker log."""
    exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
    if not exits:
        return 'incomplete (no exit code)', None
    if int(exits[-1]) != 0:
        return f'failed (exit code {exits[-1]})', None
    for marker, label in (
            ('Macro finished successfully.', 'macro completion'),
            ('Simulation finished successfully', 'simulation completion'),
            ('INFO: Finished running. These files are on the WN:', 'worker completion')):
        if marker not in text:
            return f'incomplete (missing {label})', None

    generated = re.findall(
        r'^\(New\)MuDISGenerator: Generating\s+(\d+)\s+DIS events, first event\s+(\d+)\s*$',
        text, re.MULTILINE)
    restored = re.findall(r'^Restored incoming-muon hits in (.+\.root): (\{[^\n]*\})\s*$',
                          text, re.MULTILINE)
    timing = re.findall(rf'^Real time\s+({NUMBER})\s+s, CPU time\s+({NUMBER})\s+s\s*$',
                        text, re.MULTILINE)
    outputs = re.findall(r'^Output file is\s+(.+\.root)\s*$', text, re.MULTILINE)
    if any(len(matches) != 1 for matches in (generated, restored, timing, outputs)):
        return 'incomplete (missing or ambiguous replay summary)', None
    if os.path.normpath(restored[0][0]) != os.path.normpath(outputs[0]):
        return 'incomplete (restored hits refer to a different output)', None
    try:
        hits = ast.literal_eval(restored[0][1])
    except (SyntaxError, ValueError):
        return 'incomplete (invalid restored-hit counts)', None
    if (not isinstance(hits, dict) or set(hits) != set(HIT_COLLECTIONS)
            or any(type(value) is not int or value < 0 for value in hits.values())):
        return 'incomplete (invalid restored-hit counts)', None
    real_time, cpu_time = map(float, timing[0])
    if any(not math.isfinite(value) or value < 0 for value in (real_time, cpu_time)):
        return 'incomplete (invalid simulation timing)', None
    return 'successful', {
        'generated': int(generated[0][0]),
        'first_event': int(generated[0][1]),
        'restored_hits': hits,
        'real_time': real_time,
        'cpu_time': cpu_time,
        'output': outputs[0],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path,
                        help='Directory containing numbered Ganga master-job directories')
    parser.add_argument('--first-job', type=int, required=True,
                        help='First Ganga master job ID to check')
    parser.add_argument('--num-jobs', type=int, required=True,
                        help='Number of consecutive master jobs to check')
    args = parser.parse_args()
    base = args.path.expanduser()
    if not base.is_dir():
        parser.error(f'Not a directory: {base}')
    if args.first_job < 0 or args.num_jobs <= 0:
        parser.error('--first-job must be nonnegative and --num-jobs must be positive')

    statuses = Counter()
    job_problems = Counter()
    problems = []
    results = []
    timings = []
    discovered_subjobs = 0
    for job_id in range(args.first_job, args.first_job + args.num_jobs):
        job_dir = base / str(job_id)
        if not job_dir.is_dir():
            job_problems['missing job directory'] += 1
            problems.append(f'{job_id}: missing job directory')
            continue
        try:
            logs = subjob_logs(job_dir)
        except OSError as error:
            job_problems['unreadable job directory'] += 1
            problems.append(f'{job_id}: unreadable job directory ({error})')
            continue
        if not logs:
            job_problems['no subjobs found'] += 1
            problems.append(f'{job_id}: no subjobs found')
            continue
        discovered_subjobs += len(logs)
        for subjob_id, logfile in logs:
            try:
                log_text = logfile.read_text(errors='replace')
                status, result = parse_stdout(log_text)
            except FileNotFoundError:
                status, result = 'missing stdout', None
            except OSError as error:
                status, result = f'unreadable stdout ({error})', None
            statuses[status] += 1
            if result is None:
                problems.append(f'{job_id}/{subjob_id}: {status}')
            else:
                results.append(result)
                timings.append(read_job_timing(logfile, log_text))

    print(f'Jobs {args.first_job}–{args.first_job + args.num_jobs - 1}')
    print(f'Subjobs discovered: {discovered_subjobs}')
    success_rate = (f'{100 * len(results) / discovered_subjobs:.2f}%'
                    if discovered_subjobs else 'undefined')
    print(f'Successful subjobs according to logs: {len(results)}/{discovered_subjobs} '
          f'({success_rate})')
    print('Log-only check: ROOT files and output transfers are not validated.')
    print(f'Excluded subjobs: {discovered_subjobs - len(results)}')
    for status, count in sorted(statuses.items()):
        if status != 'successful':
            print(f'  {status}: {count}')
    for status, count in sorted(job_problems.items()):
        print(f'  Job-directory problem — {status}: {count}')
    if results:
        print('\nStatistics include only successful subjobs with complete replay summaries.')
        print('SD = population standard deviation of per-subjob values about their mean.')
        print('Simulation timing is the macro timer, before incoming-muon hit restoration.')
        print(f'{"Quantity":<36} {"Sum":>15} {"Mean/subjob":>15} {"SD/subjob":>15}')
        print_stats('DIS events generated', [result['generated'] for result in results], width=36)
        for collection in HIT_COLLECTIONS:
            print_stats('Restored ' + collection,
                        [result['restored_hits'][collection] for result in results], width=36)
        print_stats('Restored hits ALL detectors',
                    [sum(result['restored_hits'].values()) for result in results], width=36)
        for key, label in (('real_time', 'Simulation real time (s)'),
                           ('cpu_time', 'Simulation CPU time (s)')):
            print_stats(label, [result[key] for result in results], width=36)
        real_time = sum(result['real_time'] for result in results)
        cpu_time = sum(result['cpu_time'] for result in results)
        ratio = f'{100 * cpu_time / real_time:.2f}%' if real_time else 'undefined'
        print(f'Simulation CPU / real time (ratio of totals): {ratio}')
        print_job_timing(timings)
    if problems:
        print('\nJobs/subjobs requiring attention:')
        print('\n'.join(problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
