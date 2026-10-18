#!/usr/bin/env python3
"""Check Ganga analyseEvents directory archives and optionally merge the results.

Example: python3 checkAnalysis.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 4000 --num-jobs 10 --merge-output /path/to/merged

Checks successful worker logs, transfers, archive structure and run.json without
opening ROOT files. --merge-output unpacks outputs and runs mergeAnalysis.py.
Unpacked inputs are retained for provenance. Failed jobs block merging unless
--merge-successful explicitly requests a partial result.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from checkPrepareEventsOutputs import eos_path, print_stats, subjob_logs


def read_analysis_archive(filename):
    """Validate transfer structure and completion metadata without loading ROOT."""
    path = Path(eos_path(filename) or filename).expanduser()
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = set()
        roots = set()
        for member in members:
            parts = Path(member.name).parts
            if (not parts or Path(member.name).is_absolute() or '..' in parts
                    or not (member.isfile() or member.isdir()) or member.name in names):
                raise ValueError(f'Unsafe or duplicate archive member: {member.name}')
            names.add(member.name)
            roots.add(parts[0])
        if len(roots) != 1:
            raise ValueError('Analysis archive must contain exactly one directory')
        root = roots.pop()
        manifest_member = archive.getmember(root + '/run.json')
        if not manifest_member.isfile() or manifest_member.size > 16 * 1024 * 1024:
            raise ValueError('Invalid or oversized run.json')
        with archive.extractfile(manifest_member) as stream:
            manifest = json.load(stream)
        if manifest.get('output_version') != 3:
            raise ValueError('Missing or unsupported analysis output version')
        kind = manifest['analysis_signature']['type']
        if kind not in ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter'):
            raise ValueError('Unknown analysis type')
        required = ['run.json', 'histograms.npz', 'histogram_flows.csv', 'normalization.csv']
        required += (['cutflow.csv', 'pair_cutflow.csv', 'pair_composition_cutflow.csv', 'pairs.csv']
                     if kind.startswith('disreplay') else ['vertex_counts.csv'])
        for name in required:
            member = archive.getmember(root + '/' + name)
            if not member.isfile() or member.size == 0:
                raise ValueError(f'Missing or empty {name}')
        processed = manifest['entries_processed']
        if type(processed) is not int or processed < 0:
            raise ValueError('Invalid entries_processed')
        if manifest['chain_entry_stop'] - manifest['chain_entry_start'] != processed:
            raise ValueError('Inconsistent chain entry range')
        if manifest['config'].get('save_plots', True) and not any(
                name.startswith(root + '/plots/') and name.endswith('.png') for name in names):
            raise ValueError('Requested plots are missing')
    return {'archive': path, 'directory': root, 'manifest': manifest}


def validate_analysis_sources(results):
    """Reject mixed settings, changed identities, and overlapping input ranges."""
    if not results:
        return
    reference = results[0]['manifest']
    intervals, identities, sizes = {}, {}, {}
    for result in results:
        manifest = result['manifest']
        if any(manifest[key] != reference[key] for key in ('analysis_signature', 'weight_definition')):
            raise ValueError('Incompatible analysis settings/code; check and merge each configuration separately')
        processed = 0
        for source in manifest['source_ranges']:
            start, stop, entries = (source[key] for key in ('start', 'stop', 'entries'))
            if any(type(value) is not int for value in (start, stop, entries)) or not 0 <= start < stop <= entries:
                raise ValueError('Invalid source range')
            path, uuid, tree = (source[key] for key in ('path', 'file_uuid', 'tree'))
            expected = 'cbmsim' if manifest['analysis_signature']['type'].startswith('disreplay') else 'MuonDIS'
            if not Path(path).is_absolute() or not uuid or tree != expected:
                raise ValueError('Invalid source identity')
            if identities.setdefault(path, uuid) != uuid:
                raise ValueError(f'Source UUID changed: {path}')
            for identity in (('path', path, tree), ('uuid', uuid, tree)):
                intervals.setdefault(identity, []).append((start, stop))
                if sizes.setdefault(identity, entries) != entries:
                    raise ValueError(f'Source entry count changed: {path}')
            processed += stop - start
        if processed != manifest['entries_processed']:
            raise ValueError('Source ranges do not match entries_processed')
    for identity, ranges in intervals.items():
        previous = -1
        for start, stop in sorted(ranges):
            if start < previous:
                raise ValueError(f'Overlapping source entries: {identity[1]}')
            previous = stop


def merge_analysis(results, args):
    """Keep unpacked inputs for provenance and invoke FairShip's existing merger."""
    destination = args.merge_output.expanduser().resolve()
    unpack = (args.unpack_dir or destination.with_name(destination.name + '_inputs')).expanduser().resolve()
    if not args.merger.is_file():
        raise ValueError(f'Merger not found: {args.merger}')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f'Merge output must be new or empty: {destination}')
    if destination == unpack or unpack in destination.parents or destination in unpack.parents:
        raise ValueError('Merge output and unpack directory must be separate')
    if unpack.exists():
        raise ValueError(f'Unpack directory already exists; choose a new --unpack-dir: {unpack}')
    unpack.mkdir(parents=True)
    inputs = []
    for index, result in enumerate(results):
        folder = unpack / str(index)
        folder.mkdir()
        with tarfile.open(result['archive'], 'r:gz') as archive:
            # read_analysis_archive has rejected traversal, links and special files.
            # Copy regular files explicitly (also works with older Python tarfile).
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if (not parts or Path(member.name).is_absolute() or '..' in parts
                        or parts[0] != result['directory']
                        or not (member.isfile() or member.isdir())):
                    raise ValueError(f'Unsafe archive member: {member.name}')
                target = folder / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
        inputs.append(str(folder / result['directory']))
    command = [sys.executable, str(args.merger), *inputs, '-o', str(destination)]
    if args.no_plots:
        command.append('--no-plots')
    print(f'Running merger on {len(inputs)} validated outputs', flush=True)
    subprocess.run(command, check=True)


def check_analysis(args, base):
    problems, results = [], []
    discovered = 0
    for job_id in range(args.first_job, args.first_job + args.num_jobs):
        job_dir = base / str(job_id)
        try:
            logs = subjob_logs(job_dir, include_unsplit=True)
            if not logs:
                raise ValueError('no subjobs found')
        except (OSError, ValueError) as error:
            problems.append(f'{job_id}: {error}')
            continue
        for subjob_id, logfile in logs:
            discovered += 1
            label = f'{job_id}/{subjob_id}'
            try:
                text = logfile.read_text(errors='replace')
                exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
                if not exits or int(exits[-1]) != 0:
                    raise ValueError('missing or nonzero exit code')
                if 'INFO: Finished running. These files are on the WN:' not in text:
                    raise ValueError('missing worker completion')
                outputs = re.findall(r'^Output: (.+)\s*$', text, re.MULTILINE)
                if len(outputs) != 1:
                    raise ValueError('missing or ambiguous analysis completion')
                locations = (logfile.parent / '__postprocesslocations__').read_text()
                if re.search(r'^massstorage\s+\S+\s+ERROR\b', locations, re.MULTILINE):
                    raise ValueError('Ganga output transfer failed')
                archives = re.findall(r'^massstorage\s+\S+\s+(.+\.tar\.gz)\s*$', locations, re.MULTILINE)
                if len(archives) != 1:
                    raise ValueError('missing or ambiguous transferred analysis archive')
                result = read_analysis_archive(archives[0].strip())
                if result['directory'] != Path(outputs[0].strip()).name:
                    raise ValueError('Transferred archive differs from analysis log output')
                # Check each manifest before including its counts.
                validate_analysis_sources([result])
                results.append(result)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, EOFError) as error:
                problems.append(f'{label}: {error}')
    print(f'Analysis jobs {args.first_job}–{args.first_job + args.num_jobs - 1}')
    print(f'Successful outputs: {len(results)}/{discovered}')
    print('Checks: worker completion, transfer, archive structure, run.json and required files; no ROOT event reads.')
    if results:
        print_stats('Analysis entries processed', [item['manifest']['entries_processed'] for item in results])
    compatible = True
    try:
        validate_analysis_sources(results)
    except (ValueError, KeyError, TypeError) as error:
        compatible = False
        problems.append(str(error))
    if problems:
        print('Jobs/outputs requiring attention:')
        print('\n'.join(problems))
    if args.merge_output:
        if not results or not compatible or (problems and not args.merge_successful):
            print('Merge not run: resolve problems or use --merge-successful to merge valid outputs only.')
            return 1
        try:
            merge_analysis(results, args)
        except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError, EOFError) as error:
            print(f'Merge failed: {error}')
            return 1
    return 1 if problems or not results else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='Directory containing numbered Ganga master jobs')
    parser.add_argument('--first-job', type=int, required=True)
    parser.add_argument('--num-jobs', type=int, required=True)
    parser.add_argument('--merge-output', type=Path, help='Run mergeAnalysis.py into this new/empty directory')
    parser.add_argument('--unpack-dir', type=Path,
                        help='New directory for retained merge inputs (default: MERGE_OUTPUT_inputs)')
    parser.add_argument('--merger', type=Path,
                        default=Path('/afs/cern.ch/work/a/ammagnan/FairShip/newMuonDIS/mergeAnalysis.py'))
    parser.add_argument('--no-plots', action='store_true', help='Skip rendering merged plots')
    parser.add_argument('--merge-successful', action='store_true',
                        help='Explicitly allow a partial merge of valid jobs')
    args = parser.parse_args()
    if not args.merge_output and (args.unpack_dir or args.no_plots or args.merge_successful):
        parser.error('--unpack-dir, --no-plots and --merge-successful require --merge-output')
    base = args.path.expanduser()
    if not base.is_dir():
        parser.error(f'Not a directory: {base}')
    if args.first_job < 0 or args.num_jobs <= 0:
        parser.error('--first-job must be nonnegative and --num-jobs must be positive')
    args.merger = args.merger.expanduser().resolve()
    return check_analysis(args, base)


if __name__ == '__main__':
    sys.exit(main())
