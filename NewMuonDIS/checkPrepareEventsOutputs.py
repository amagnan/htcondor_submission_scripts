#!/usr/bin/env python3
"""Check all Ganga subjobs of a newMuonDIS prepareEvents production.

Example: python3 checkPrepareEventsOutputs.py \
    ~/gangadir/workspace/ammagnan/LocalXML --first-job 2200 --num-jobs 50

The argument contains numbered Ganga master-job directories.  The script
discovers each job's ``JOB_ID/SUBJOB_ID/output/stdout`` files, reads the EOS
destination recorded by Ganga, and does not modify files.

All recorded ROOT outputs excluded from the summary statistics are written as
safely quoted ``rm`` commands to an executable ``rmcommand.sh`` in the current
working directory, including outputs of failed or incomplete jobs.
"""

import argparse
from collections import Counter
from contextlib import redirect_stderr
from pathlib import Path
import math
import os
import re
import shlex
import statistics
import sys
import tempfile
from urllib.parse import urlsplit


WDIS_SCALE = 0.006*1e-27 * 6.02214076e23
MATERIALS = ('MS', 'UBT', 'SBTsens', 'SBTfr', 'SSTsens', 'SSTfr', 'HE', 'AIR',
             'REST')


def natural_key(path):
    """Sort Ganga subjob names numerically where possible."""
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def subjob_logs(job_dir):
    """Return LocalXML (subjob label, stdout path) pairs for a Ganga job."""
    directories = [item for item in job_dir.iterdir()
                   if item.is_dir() and item.name.isdigit()]
    return [(directory.name, directory / 'output/stdout')
            for directory in sorted(directories, key=natural_key)]


def output_locations(subjob_dir):
    """Read all ROOT output locations recorded by Ganga's mass-storage handler."""
    try:
        text = (subjob_dir / 'output/__postprocesslocations__').read_text(
            errors='replace')
    except FileNotFoundError:
        return []
    locations = re.findall(r'^massstorage\s+\S+\s+(\S+\.root)\s*$', text,
                           re.MULTILINE)
    return list(dict.fromkeys(locations))


def parse_stdout(text, output):
    """Return log-derived counts and the transferred ROOT-file path."""
    exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
    if not exits:
        return 'incomplete (no exit code)', None
    if int(exits[-1]) != 0:
        return f'failed (exit code {exits[-1]})', None
    if 'INFO: Finished running. These files are on the WN:' not in text:
        return 'incomplete (missing worker completion)', None

    input_match = re.findall(r'\* input tree with (\d+) entries', text)
    range_match = re.findall(r'- Processing event (\d+) to event (-?\d+)', text)
    summary_match = re.findall(
        r'Found (\d+) mu\+ and (\d+) mu-\.\s*\nSkipped: (\d+) events and '
        r'(\d+) muons with too low p, (\d+) muons outside of acceptance\.', text)
    if (len(input_match) != 1 or len(range_match) != 1 or len(summary_match) != 1
            or output is None):
        return 'incomplete (missing or ambiguous prepareEvents summary/output)', None

    first, last = map(int, range_match[0])
    input_entries = int(input_match[0])
    processed = max(0, last - first + 1)
    if first < 0 or last >= input_entries:
        return 'incomplete (invalid processed range)', None
    positive, negative, skipped_events, skipped_low_p, skipped_acceptance = map(
        int, summary_match[0])
    return 'successful log', {
        'processed': processed,
        'mu_plus': positive,
        'mu_minus': negative,
        'skipped_events': skipped_events,
        'skipped_low_p': skipped_low_p,
        'skipped_acceptance': skipped_acceptance,
        'output': output,
    }


def root_statistics(filename):
    """Open a MuonDIS output and return its counts, or a diagnostic string."""
    try:
        import ROOT
    except ImportError as error:
        return f'cannot import PyROOT: {error}', None

    # ROOT reports some recovery/read failures only through C++ stderr, even
    # when Open/Get/Draw return usable objects or plausible entry counts.
    with tempfile.TemporaryFile(mode='w+') as diagnostics:
        sys.stderr.flush()
        saved_stderr = os.dup(2)
        try:
            os.dup2(diagnostics.fileno(), 2)
            with redirect_stderr(diagnostics):
                try:
                    status, result = read_root_statistics(ROOT, filename)
                except (OSError, RuntimeError, ValueError, OverflowError) as error:
                    status, result = f'corrupt or unreadable ROOT file ({error})', None
                finally:
                    diagnostics.flush()
        finally:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        diagnostics.seek(0)
        messages = diagnostics.read()
    sys.stderr.write(messages)
    failures = [line for line in messages.splitlines()
                if re.search(r'not closed|trying to recover|recovered \d+ keys|'
                             r'::Recover\b|\bError in <|\bFatal in <', line,
                             re.IGNORECASE)]
    if failures:
        return f'corrupt or unreadable ROOT file ({failures[0].strip()})', None
    return status, result


def read_root_statistics(ROOT, filename):
    """Read the statistics while root_statistics monitors ROOT diagnostics."""
    root_file = ROOT.TFile.Open(filename, 'READ')
    if not root_file:
        return 'corrupt or unreadable ROOT file', None
    try:
        if root_file.IsZombie():
            return 'corrupt or unreadable ROOT file', None
        if root_file.TestBit(ROOT.TFile.kRecovered):
            return 'corrupt ROOT file (recovered keys)', None
        tree = root_file.Get('MuonDIS')
        if not tree or not tree.InheritsFrom('TTree'):
            return 'corrupt ROOT file (missing MuonDIS tree)', None
        branches = ['muon_nDISevt_' + material for material in MATERIALS]
        branches += ['muon_wDIS_' + material for material in MATERIALS]
        missing = [branch for branch in branches if not tree.GetBranch(branch)]
        if missing:
            return 'corrupt ROOT file (missing branches: ' + ', '.join(missing) + ')', None

        # Opening the file and reading every scalar statistics branch catches
        # malformed metadata and the baskets needed for the reported counts,
        # without loading the multi-GB vector branches in every output.
        materials = {}
        for material in MATERIALS:
            raw_expression = 'muon_nDISevt_' + material
            weighted_expression = f'muon_wDIS_{material}*{raw_expression}'
            raw_read = tree.Draw(raw_expression, '', 'goff')
            if raw_read != tree.GetEntries():
                return f'corrupt ROOT tree (cannot read {raw_expression})', None
            raw = int(round(sum(tree.GetV1()[index] for index in range(raw_read))))
            weighted_read = tree.Draw(weighted_expression, '', 'goff')
            if weighted_read != tree.GetEntries():
                return f'corrupt ROOT tree (cannot read {weighted_expression})', None
            weighted = sum(tree.GetV1()[index] for index in range(weighted_read))
            if not math.isfinite(weighted):
                return f'corrupt ROOT tree (non-finite {weighted_expression})', None
            materials[material] = (raw, weighted * WDIS_SCALE)
        return 'successful', {'written': int(tree.GetEntries()), 'materials': materials}
    finally:
        root_file.Close()


def eos_path(filename):
    """Convert a recorded EOS path or XRootD URL to a mounted EOS path."""
    path = urlsplit(filename).path if filename.startswith('root://') else filename
    if not path.startswith('/'):
        return None
    path = '/' + path.lstrip('/')
    return path if path.startswith('/eos/') else None


def print_stats(label, values):
    print(f'{label:<30} {sum(values):>15.8g} {statistics.mean(values):>15.8g} '
          f'{statistics.pstdev(values):>15.8g}')


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
    excluded_outputs = []
    included_outputs = set()
    excluded_without_output = 0
    discovered_subjobs = 0
    for job_id in range(args.first_job, args.first_job + args.num_jobs):
        job_dir = base / str(job_id)
        if not job_dir.is_dir():
            job_problems['missing job directory'] += 1
            problems.append(f'{job_id}: missing job directory')
            continue
        logs = subjob_logs(job_dir)
        if not logs:
            job_problems['no subjobs found'] += 1
            problems.append(f'{job_id}: no subjobs found')
            continue
        discovered_subjobs += len(logs)
        for subjob_id, logfile in logs:
            label = f'{job_id}/{subjob_id}'
            outputs = []
            try:
                outputs = output_locations(job_dir / subjob_id)
            except OSError as error:
                problems.append(f'{label}: unreadable output locations ({error})')
            output = outputs[0] if len(outputs) == 1 else None
            try:
                log_status, log_result = parse_stdout(logfile.read_text(errors='replace'), output)
            except FileNotFoundError:
                log_status, log_result = 'missing stdout', None
            except OSError as error:
                log_status, log_result = f'unreadable stdout ({error})', None
            # Failed/incomplete worker logs can still have a transferred file.
            # Check it as well so corrupt outputs are not silently omitted.
            root_status, root_result = None, None
            for filename in outputs:
                root_status, root_result = root_statistics(filename)
                if root_result is None:
                    problems.append(f'{label}: {root_status}: {filename}')
            if log_result is None or root_result is None:
                # Count each excluded subjob once; retain additional ROOT
                # diagnostics in the detailed problems list below.
                status = log_status if log_result is None else root_status
                statuses[status] += 1
                if log_result is None:
                    problems.append(f'{label}: {log_status}')
                if not outputs:
                    excluded_without_output += 1
                    problems.append(f'{label}: no recorded ROOT output path for rm')
                excluded_outputs.extend(outputs)
                continue
            statuses['successful'] += 1
            log_result.update(root_result)
            results.append(log_result)
            included_outputs.add(eos_path(output) or output)

    print(f'Jobs {args.first_job}–{args.first_job + args.num_jobs - 1}')
    print(f'Subjobs discovered: {discovered_subjobs}')
    success_rate = (f'{100 * len(results) / discovered_subjobs:.2f}%'
                    if discovered_subjobs else 'undefined')
    print(f'Successful subjobs with valid ROOT output: {len(results)}/{discovered_subjobs} '
          f'({success_rate})')
    print(f'Excluded subjobs: {discovered_subjobs - len(results)} '
          '(one primary reason per subjob below)')
    for status, count in sorted(statuses.items()):
        if status != 'successful':
            print(f'  {status}: {count}')
    print(f'Excluded subjobs without a recorded ROOT output path: {excluded_without_output}')
    for status, count in sorted(job_problems.items()):
        print(f'  Job-directory problem — {status}: {count}')
    if results:
        print('\nStatistics include only successful subjobs with valid MuonDIS output.')
        print(f'All weighted DIS counts scaled by {WDIS_SCALE:.8g} (1e-27 × 6.02214076e23).')
        print('SD = population standard deviation of per-subjob counts about their mean.')
        print(f'{"Quantity":<30} {"Sum":>15} {"Mean/subjob":>15} {"SD/subjob":>15}')
        for key, label in (('processed', 'Input entries processed'),
                           ('written', 'MuonDIS entries written'),
                           ('mu_plus', 'Muons mu+'), ('mu_minus', 'Muons mu-'),
                           ('skipped_events', 'Skipped malformed/non-muon'),
                           ('skipped_low_p', 'Skipped low-p muons'),
                           ('skipped_acceptance', 'Skipped acceptance')):
            print_stats(label, [result[key] for result in results])
        processed = sum(result['processed'] for result in results)
        written = sum(result['written'] for result in results)
        print(f'MuonDIS written: {written}/{processed}'
              + (f' ({100 * written / processed:.4f}%)' if processed else ' (undefined fraction)'))
        for material in MATERIALS:
            print_stats('DIS ' + material + ' raw',
                        [result['materials'][material][0] for result in results])
            print_stats('DIS ' + material + ' weighted',
                        [result['materials'][material][1] for result in results])
        for index, label in enumerate(('raw', 'weighted')):
            print_stats('DIS ALL materials ' + label,
                        [sum(value[index] for value in result['materials'].values())
                         for result in results])
    if problems:
        print('\nSubjobs requiring attention:')
        print('\n'.join(problems))
    rm_command = Path.cwd() / 'rmcommand.sh'
    unique_excluded_outputs = list(dict.fromkeys(
        eos_path(filename) or filename for filename in excluded_outputs
        if (eos_path(filename) or filename) not in included_outputs))
    removal_paths = [filename for filename in unique_excluded_outputs
                     if eos_path(filename) is not None]
    print(f'Unique ROOT outputs included in statistics: {len(included_outputs)}')
    print(f'Unique recorded ROOT outputs excluded from statistics: '
          f'{len(unique_excluded_outputs)}')
    for filename in unique_excluded_outputs:
        if eos_path(filename) is None:
            print(f'No absolute EOS path for rm: {filename}')
    commands = ['#!/usr/bin/env bash', 'set -euo pipefail']
    commands.extend(f'rm -- {shlex.quote(filename)}'
                    for filename in removal_paths)
    rm_command.write_text('\n'.join(commands) + '\n')
    rm_command.chmod(0o755)
    print(f'Wrote {len(removal_paths)} excluded-output deletion command(s) to '
          f'{rm_command}')
    if problems:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
