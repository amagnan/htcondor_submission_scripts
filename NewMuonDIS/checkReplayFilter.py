#!/usr/bin/env python3
"""Check replayfilter logs, transfers, complete masters and selection efficiency.

No ROOT event reads. Validates the worker's counts/identities and transferred
file sizes, including valid ROOT outputs with zero selected events.
Complete preparation metadata is required. Add --metadata to print its totals.
"""

import argparse
import json
from pathlib import Path
import re
import sys

from checkPrepareEventsOutputs import (eos_path, print_normalization_statistics,
                                       root_statistics, subjob_logs)
from replayProduction import complete_masters, read_filter_manifest


def read_checked_output(logfile, check_metadata=True):
    """Validate one filter worker log, transfers and its output manifest."""
    text = logfile.read_text(errors='replace')
    exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
    if not exits or int(exits[-1]) != 0 or 'INFO: Finished running.' not in text:
        raise ValueError('missing or unsuccessful worker completion')
    names = re.findall(r'^Replayfilter manifest: (.+)$', text, re.MULTILINE)
    if len(names) != 1:
        raise ValueError('missing or ambiguous filter completion')
    locations = (logfile.parent / '__postprocesslocations__').read_text()
    if re.search(r'^massstorage\s+\S+\s+ERROR\b', locations, re.MULTILINE):
        raise ValueError('Ganga output transfer failed')
    files = [Path(eos_path(item) or item) for item in re.findall(
        r'^massstorage\s+\S+\s+(.+?)\s*$', locations, re.MULTILINE)]
    manifests = [file for file in files if file.suffix == '.json']
    if len(manifests) != 1 or manifests[0].name != names[0].strip():
        raise ValueError('missing or ambiguous transferred filter manifest')
    path = manifests[0]
    record = read_filter_manifest(path)
    expected = {path, path.parent / record['output'], path.parent / record['geometry']}
    if set(files) != expected:
        raise ValueError('transfer record does not contain exactly ROOT, geometry and manifest')
    if check_metadata:
        filename = str(path.parent / record['output'])
        status, result = root_statistics(filename, metadata_only=True, tree_name='cbmsim')
        if result is None:
            raise ValueError(f'{filename}: {status}')
        record = dict(record, normalization=result['normalization'])
    return path, record


def check(args):
    records, problems = [], []
    metadata_results = []
    approved = []
    discovered = 0
    for job_id in range(args.first_job, args.first_job + args.num_jobs):
        try:
            logs = subjob_logs(args.path / str(job_id), include_unsplit=True)
            if not logs:
                raise ValueError('no subjobs found')
        except (OSError, ValueError) as error:
            problems.append(f'{job_id}: {error}')
            continue
        for child, logfile in logs:
            discovered += 1
            try:
                path, record = read_checked_output(logfile)
                metadata_results.append({'normalization': record['normalization']})
                records.append((path, record))
                if not record['production']['test']:
                    approved.append(dict(job_id=job_id, subjob_id=child, manifest=str(path),
                                         output=str(path.parent / record['output'])))
            except (OSError, ValueError, KeyError, TypeError) as error:
                problems.append(f'{job_id}/{child}: {error}')
    print(f'Replayfilter successful outputs: {len(records)}/{discovered}')
    print('Checks: worker completion, transfers, manifests, file sizes and complete master groups; no ROOT event reads.')
    print('Complete preparation metadata required for every included output.')
    try:
        masters = complete_masters(records)
        print(f'Complete filtered disgen files: {len(masters)}')
    except ValueError as error:
        problems.append(str(error))
    before = sum(record['filter']['events_processed'] for _, record in records)
    tracks = sum(record['filter']['events_two_tracks'] for _, record in records)
    selected = sum(record['filter']['events_selected'] for _, record in records)
    print(f'Valid outputs: processed {before}; >=2 tracks {tracks}; selected {selected}')
    print('Selection efficiency: ' + (f'{100 * selected / before:.6g}%' if before else 'undefined (no input events)'))
    print(f'Filtered ROOT size: {sum(record["output_bytes"] for _, record in records)} bytes')
    original_bytes = sum(record.get('source_bytes', 0) for _, record in records)
    if original_bytes:
        selected_bytes = sum(record['output_bytes'] for _, record in records)
        print(f'Filtered/original ROOT bytes: {selected_bytes}/{original_bytes} ({100 * selected_bytes / original_bytes:.4g}%)')
    if getattr(args, 'metadata', False):
        print('Metadata mode: ROOT headers checked; event baskets not validated.')
        try:
            print_normalization_statistics(metadata_results)
        except ValueError as error:
            problems.append(str(error))
    if problems:
        print('Jobs/outputs requiring attention:\n' + '\n'.join(problems))
    export = getattr(args, 'export_valid_inputs', None)
    if export:
        # Overlapping masters/sources cannot form an unambiguous analysis list.
        complete_masters(records)
        export.write_text(json.dumps(dict(version=1, workspace=str(args.path.resolve()),
                                          inputs=approved), indent=2) + '\n')
        print(f'Exported {len(approved)} completed non-test filter outputs to {export}')
    return int(bool(problems) or not records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='Directory containing numbered Ganga master jobs')
    parser.add_argument('--first-job', required=True, type=int)
    parser.add_argument('--num-jobs', required=True, type=int)
    parser.add_argument('--metadata', action='store_true',
                        help='Print normalization totals (preparation metadata is always checked)')
    parser.add_argument('--export-valid-inputs', type=Path,
                        help='Write an analysis input list containing only validated non-test filter outputs')
    args = parser.parse_args()
    args.path = args.path.expanduser()
    if not args.path.is_dir() or args.first_job < 0 or args.num_jobs < 1:
        parser.error('Require an existing path, nonnegative first job and positive number of jobs')
    return check(args)


if __name__ == '__main__':
    sys.exit(main())
