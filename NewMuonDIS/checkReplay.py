#!/usr/bin/env python3
"""Check Ganga NewMuDIS replay logs and summarize events, restored hits and timing.

Example: python3 checkReplay.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 3225 --num-jobs 1

Reads JOB_ID/SUBJOB_ID/output/stdout. Only successful subjobs with complete
summaries and complete preparation metadata contribute to statistics. ROOT
headers are checked without reading event baskets. --export-valid-inputs
additionally checks transfers and nonempty files, exporting only masters with
every expected subjob successful. Add --metadata to print normalization totals.
"""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import math
import os
import re
import sys

from checkPrepareEventsOutputs import (NUMBER, eos_path, output_locations,
                                       print_normalization_statistics, print_stats,
                                       root_statistics, subjob_logs)
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


def valid_master_inputs(job_dir, expected_subjobs, check_metadata=True):
    """Return transferred replay files only when the entire master passes."""
    if type(expected_subjobs) is not int or expected_subjobs < 1:
        raise ValueError('expected_subjobs must be a positive integer')
    logs = subjob_logs(job_dir)
    if {child for child, _ in logs} != {str(index) for index in range(expected_subjobs)}:
        raise ValueError(f'expected exactly subjobs 0–{expected_subjobs - 1}')
    sources = []
    for child, logfile in logs:
        status, result = parse_stdout(logfile.read_text(errors='replace'))
        if result is None:
            raise ValueError(f'subjob {child}: {status}')
        locations = (logfile.parent / '__postprocesslocations__').read_text()
        if re.search(r'^massstorage\s+\S+\s+ERROR\b', locations, re.MULTILINE):
            raise ValueError(f'subjob {child}: output transfer failed')
        files = [Path(eos_path(item) or item) for item in re.findall(
            r'^massstorage\s+\S+\s+(\S+\.root)\s*$', locations, re.MULTILINE)]
        matches = [path for path in files if path.name == Path(result['output']).name]
        if len(matches) != 1:
            raise ValueError(f'subjob {child}: missing or ambiguous replay transfer')
        source = matches[0]
        if (not source.is_absolute() or not source.name.startswith('sim_')
                or not source.name.endswith(('_disreplay_gen.root', '_disreplay_filter.root'))
                or source.parent.name != child):
            raise ValueError(f'subjob {child}: unexpected replay output path {source}')
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError(f'subjob {child}: missing or empty replay output {source}')
        if check_metadata:
            status, metadata = root_statistics(str(source), metadata_only=True, tree_name='cbmsim')
            if metadata is None:
                raise ValueError(f'subjob {child}: {source}: {status}')
        sources.append(str(source))
    if len({Path(source).parent.parent for source in sources}) != 1:
        raise ValueError('subjobs do not belong to one prepared-input directory')
    return sorted(sources)


def export_valid_inputs(base, first_job, num_jobs, expected_subjobs, destination):
    masters, excluded = [], []
    for job_id in range(first_job, first_job + num_jobs):
        try:
            sources = valid_master_inputs(base / str(job_id), expected_subjobs)
            masters.append(dict(job_id=job_id, expected_subjobs=expected_subjobs, sources=sources))
        except (OSError, ValueError) as error:
            excluded.append(dict(job_id=job_id, reason=str(error)))
    record = dict(version=1, workspace=str(base.resolve()), masters=masters, excluded=excluded)
    destination.write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path,
                        help='Directory containing numbered Ganga master-job directories')
    parser.add_argument('--first-job', type=int, required=True,
                        help='First Ganga master job ID to check')
    parser.add_argument('--num-jobs', type=int, required=True,
                        help='Number of consecutive master jobs to check')
    parser.add_argument('--metadata', action='store_true',
                        help='Print normalization totals (preparation metadata is always checked)')
    parser.add_argument('--export-valid-inputs', type=Path,
                        help='Write replayfilter input JSON containing only complete successful masters')
    parser.add_argument('--expected-subjobs', type=int,
                        help='Required with export: exact number of subjobs per replay master')
    args = parser.parse_args()
    base = args.path.expanduser()
    if not base.is_dir():
        parser.error(f'Not a directory: {base}')
    if args.first_job < 0 or args.num_jobs <= 0:
        parser.error('--first-job must be nonnegative and --num-jobs must be positive')
    if args.export_valid_inputs and (args.expected_subjobs is None or args.expected_subjobs < 1):
        parser.error('--export-valid-inputs requires --expected-subjobs >= 1')

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
                if result is not None:
                    outputs = [filename for filename in output_locations(logfile.parent.parent)
                               if Path(filename).name == Path(result['output']).name]
                    if len(outputs) != 1:
                        status, result = 'missing or ambiguous transferred replay output', None
                    else:
                        root_status, root_result = root_statistics(
                            outputs[0], metadata_only=True, tree_name='cbmsim')
                        if root_result is None:
                            status, result = f'{outputs[0]}: {root_status}', None
                        else:
                            result.update(root_result)
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
    success_label = 'Successful subjobs with ROOT headers checked'
    print(f'{success_label}: {len(results)}/{discovered_subjobs} '
          f'({success_rate})')
    print('Complete preparation metadata required; ROOT headers checked; event baskets not validated.')
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
        if args.metadata:
            try:
                print_normalization_statistics(results)
            except ValueError as error:
                problems.append(str(error))
    if problems:
        print('\nJobs/subjobs requiring attention:')
        print('\n'.join(problems))
    if args.export_valid_inputs:
        record = export_valid_inputs(base, args.first_job, args.num_jobs,
                                     args.expected_subjobs, args.export_valid_inputs)
        print(f'Exported {len(record["masters"])} complete masters, '
              f'{sum(len(master["sources"]) for master in record["masters"])} replay files '
              f'to {args.export_valid_inputs}')
        print('Export checks: all expected subjobs successful, transfer records and nonempty files; no ROOT event reads.')
        for master in record['excluded']:
            print(f'Excluded master {master["job_id"]}: {master["reason"]}')
        if record['excluded'] or not record['masters']:
            return 1
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
