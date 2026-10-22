#!/usr/bin/env python3
"""Check all Ganga subjobs of a newMuonDIS prepareEvents production.

Example: python3 checkPrepareEventsOutputs.py \
    ~/gangadir/workspace/ammagnan/LocalXML --first-job 2200 --num-jobs 50

The argument contains numbered Ganga master-job directories.  The script
discovers each job's ``JOB_ID/SUBJOB_ID/output/stdout`` files, reads the EOS
destination recorded by Ganga, and does not modify files.

By default, only local logs and output-location records are read; ROOT files
are not opened. Add --metadata for tree-header normalization records only,
or --check-root to validate outputs and include ROOT-derived
entry counts, muon weights, and DIS statistics.

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

from jobTiming import print_job_timing, read_job_timing


WDIS_SCALE = 0.006 * 1e-27 * 6.02214076e23
MATERIALS = ('MS', 'UBT', 'SBTsens', 'SBTfr', 'SSTsens', 'SSTfr', 'HE', 'AIR',
             'HeBalloon', 'REST')
NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'


def parse_path_diagnostics(text):
    """Read the five diagnostic lines printed by MuGeoProcessor."""
    patterns = (
        r'Muon path diagnostics: (?P<considered>\d+) muons considered, '
        r'(?P<non_forward>\d+) rejected for non-forward momentum, '
        r'(?P<invalid_measurements>\d+) rejected for invalid measurements',
        r'Muon path routes prepared \(before material navigation\): '
        r'(?P<ubt_backward>\d+) from UBT with backward extrapolation, '
        r'(?P<mc_field>\d+) from MC start in field, '
        r'(?P<mc_field_free>\d+) from MC start in field-free region '
        r'\(bending on field entry\); (?P<beyond_zmax>\d+) skipped with start z >= maximum z',
        r'UBT backward paths missing MS: (?P<ubt_missing_ms>\d+)',
        rf'Muons starting before z = (?P<z_min_cm>{NUMBER}) cm '
        r'\(paths still built\): (?P<before_zmin>\d+)',
        r'POCA transverse jumps: (?P<large_jumps>\d+) of (?P<transitions>\d+) '
        rf'transitions exceed (?P<jump_threshold_cm>{NUMBER}) cm, in '
        r'(?P<muons_with_large_jumps>\d+) muons; '
        rf'maximum jump = (?P<max_jump_cm>{NUMBER}) cm',
    )
    diagnostics = {}
    for pattern in patterns:
        matches = list(re.finditer(pattern, text))
        if len(matches) != 1:
            return None
        for key, value in matches[0].groupdict().items():
            diagnostics[key] = float(value) if key.endswith('_cm') else int(value)
    if any(not math.isfinite(value) for key, value in diagnostics.items()
           if key.endswith('_cm')):
        return None
    return diagnostics


def parse_extra_diagnostics(text):
    """Accept legacy logs; reject partial/duplicate new diagnostic blocks."""
    patterns = (
        rf'Weighted counts \(sum MCTrack\[0\]\.fW\): (?P<mu_plus>{NUMBER}) mu\+ and '
        rf'(?P<mu_minus>{NUMBER}) mu- \(after momentum cut\)\.',
        rf'Skipped \(sum MCTrack\[0\]\.fW\): (?P<low_p>{NUMBER}) with too low p, '
        rf'(?P<invalid_paths>{NUMBER}) muons with invalid or empty paths\.',
        rf'Counted \(sum MCTrack\[0\]\.fW\): (?P<no_hits>{NUMBER}) '
        r'muons with no recorded hits in UBT,SBT,SST,TD\.',
        rf'Muon path diagnostics \(sum MCTrack\[0\]\.fW\): (?P<considered>{NUMBER}) considered, '
        rf'(?P<non_forward>{NUMBER}) rejected for non-forward momentum, '
        rf'(?P<invalid_measurements>{NUMBER}) rejected for invalid measurements',
        rf'Muon path routes prepared \(sum MCTrack\[0\]\.fW\): (?P<ubt_backward>{NUMBER}) from UBT, '
        rf'(?P<mc_field>{NUMBER}) from MC start in field, '
        rf'(?P<mc_field_free>{NUMBER}) from MC start in field-free region, '
        rf'(?P<beyond_zmax>{NUMBER}) skipped with start z >= maximum z',
        rf'UBT backward paths missing MS \(sum MCTrack\[0\]\.fW\): (?P<ubt_missing_ms>{NUMBER})',
        rf'Muons starting before z = {NUMBER} cm \(sum MCTrack\[0\]\.fW\): (?P<before_zmin>{NUMBER})',
        rf'Muons with large POCA transverse jumps \(sum MCTrack\[0\]\.fW\): (?P<large_jumps>{NUMBER})',
    )
    result = {}
    if '(sum MCTrack[0].fW)' in text:
        weighted = {}
        for pattern in patterns:
            matches = list(re.finditer(pattern, text))
            if len(matches) != 1:
                raise ValueError('missing or ambiguous weighted diagnostics')
            weighted.update({key: float(value) for key, value in matches[0].groupdict().items()})
        if not all(math.isfinite(value) for value in weighted.values()):
            raise ValueError('non-finite weighted diagnostics')
        result['weighted_diagnostics'] = weighted
    if 'Muon path totals' in text:
        totals = {}
        for material in ('ALL', *MATERIALS):
            prefix = (r'\(cm, excluding REST\): output' if material == 'ALL'
                      else re.escape(material) + r' \(cm\): crossing')
            pattern = (rf'Muon path totals {prefix} muons = (?P<muons>\d+), '
                       rf'sum\(fW\) = (?P<weight>{NUMBER}), sum\(L\) = (?P<length>{NUMBER}), '
                       rf'sum\(fW\*L\) = (?P<weighted_length>{NUMBER})')
            matches = list(re.finditer(pattern, text))
            if len(matches) != 1:
                raise ValueError('missing or ambiguous path totals')
            values = {key: int(value) if key == 'muons' else float(value)
                      for key, value in matches[0].groupdict().items()}
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError('non-finite path totals')
            totals[material] = values
        result['path_totals'] = totals
    return result


def read_normalization(tree):
    """Read version-1 TList metadata without reading any event baskets."""
    metadata = tree.GetUserInfo().FindObject('muDIS_normalization')
    if not metadata:
        return None
    version = metadata.FindObject('version')
    if not version or int(version.GetVal()) != 1:
        raise ValueError('unsupported normalization metadata version')
    complete = metadata.FindObject('preparation_complete')
    records = {}
    for item in metadata:
        if not item.InheritsFrom('TList'):
            continue
        stage = item.FindObject('stage')
        if not stage or stage.GetTitle() not in ('prepareEvents', 'filterEvents', 'NewMuDISGenerator'):
            raise ValueError('invalid normalization stage')
        record = {'stage': str(stage.GetTitle())}
        for key in ('input_muons', 'sum_weights', 'sum_weights_squared'):
            value = item.FindObject(key)
            if not value:
                raise ValueError(f'missing normalization field {key}')
            record[key] = int(value.GetVal()) if key == 'input_muons' else float(value.GetVal())
            if not math.isfinite(record[key]) or (key != 'sum_weights' and record[key] < 0):
                raise ValueError(f'invalid normalization field {key}')
        identity = str(item.GetName())
        if identity in records and records[identity] != record:
            raise ValueError('conflicting normalization UUID')
        records[identity] = record
    return {'complete': bool(complete and complete.GetVal()), 'records': records}


def print_extra_statistics(results):
    weighted = [result['weighted_diagnostics'] for result in results if 'weighted_diagnostics' in result]
    if weighted:
        print(f'\nWeighted diagnostics: {len(weighted)}/{len(results)} subjobs; sum MCTrack[0].fW (no DIS scaling).')
        for key in weighted[0]:
            print_stats(key.replace('_', ' '), [item[key] for item in weighted])
    totals = [result['path_totals'] for result in results if 'path_totals' in result]
    if totals:
        print(f'\nPath lengths: {len(totals)}/{len(results)} subjobs; means are ratios of combined totals.')
        print('ALL excludes REST; material means use crossing muons only.')
        for material in ('ALL', *MATERIALS):
            combined = {key: math.fsum(item[material][key] for item in totals)
                        for key in ('muons', 'weight', 'length', 'weighted_length')}
            mean = combined['length'] / combined['muons'] if combined['muons'] else None
            weighted_mean = combined['weighted_length'] / combined['weight'] if combined['weight'] else None
            print(f'{material}: sum(L)={combined["length"]:.8g} cm, '
                  f'sum(fW*L)={combined["weighted_length"]:.8g} cm, '
                  f'mean L={mean if mean is not None else "undefined"}, '
                  f'weighted mean L={weighted_mean if weighted_mean is not None else "undefined"} cm')


def print_normalization_statistics(results):
    records = {}
    available = complete = 0
    for result in results:
        metadata = result.get('normalization')
        if metadata is None:
            continue
        available += 1
        complete += metadata['complete']
        for identity, record in metadata['records'].items():
            if identity in records and records[identity] != record:
                raise ValueError(f'conflicting normalization UUID: {identity}')
            records[identity] = record
    print(f'\nNormalization metadata: {available}/{len(results)} outputs; '
          f'preparation complete: {complete}/{len(results)}. Records deduplicated by UUID.')
    for stage in ('prepareEvents', 'filterEvents', 'NewMuDISGenerator'):
        selected = [record for record in records.values() if record['stage'] == stage]
        if selected:
            print(stage + ' (input muons before selection): ' + ', '.join(
                f'{key}={math.fsum(record[key] for record in selected):.12g}'
                for key in ('input_muons', 'sum_weights', 'sum_weights_squared')))

def natural_key(path):
    """Sort Ganga subjob names numerically where possible."""
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def subjob_logs(job_dir, include_unsplit=False):
    """Return LocalXML (subjob label, stdout path) pairs for a Ganga job."""
    directories = [item for item in job_dir.iterdir()
                   if item.is_dir() and item.name.isdigit()]
    if include_unsplit and not directories and (job_dir / 'output').is_dir():
        return [('.', job_dir / 'output/stdout')]
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
        r'(\d+) muons with too low p\s*\nCounted: (\d+) muons with no recorded '
        r'hits in UBT,SBT,SST,TD\.', text)
    if (len(input_match) != 1 or len(range_match) != 1 or len(summary_match) != 1
            or output is None):
        return 'incomplete (missing or ambiguous prepareEvents summary/output)', None

    first, last = map(int, range_match[0])
    input_entries = int(input_match[0])
    processed = max(0, last - first + 1)
    if first < 0 or last >= input_entries:
        return 'incomplete (invalid processed range)', None
    positive, negative, skipped_events, skipped_low_p, no_hits = map(int, summary_match[0])
    diagnostics = parse_path_diagnostics(text)
    if diagnostics is None:
        return 'incomplete (missing, ambiguous or invalid muon path diagnostics)', None
    try:
        extra = parse_extra_diagnostics(text)
    except ValueError as error:
        return f'incomplete ({error})', None
    return 'successful log', {
        **extra,
        'processed': processed,
        'mu_plus': positive,
        'mu_minus': negative,
        'skipped_events': skipped_events,
        'skipped_low_p': skipped_low_p,
        'no_hits': no_hits,
        'path_diagnostics': diagnostics,
        'output': output,
    }


def root_statistics(filename, weights_only=False, metadata_only=False, include_metadata=False):
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
                    if metadata_only:
                        status, result = read_root_statistics(ROOT, filename, metadata_only=True)
                    elif include_metadata:
                        status, result = read_root_statistics(ROOT, filename, include_metadata=True)
                    elif weights_only:
                        status, result = read_root_statistics(ROOT, filename, weights_only=True)
                    else:
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


def read_muon_weight_sum(tree):
    """Sum the first track's weight once per muon, excluding daughter tracks."""
    branch = 'muon_MCTracks.fW'
    if not tree.GetBranch(branch):
        raise ValueError(f'missing branch {branch}')
    # Bound Draw's result buffer and read only the split weight branch.
    chunk_size = 10000
    tree.SetEstimate(chunk_size + 1)
    totals = []
    for first in range(0, int(tree.GetEntries()), chunk_size):
        count = min(chunk_size, int(tree.GetEntries()) - first)
        read = tree.Draw(branch + '[0]', '', 'goff', count, first)
        if read != count:
            raise ValueError(f'cannot read one muon weight per entry from {branch}')
        values = [tree.GetV1()[index] for index in range(read)]
        if any(not math.isfinite(value) for value in values):
            raise ValueError(f'non-finite {branch}')
        totals.append(math.fsum(values))
    return math.fsum(totals)


def read_root_statistics(ROOT, filename, weights_only=False, metadata_only=False, include_metadata=False):
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
        if metadata_only:
            return 'successful', {'written': int(tree.GetEntries()),
                                  'normalization': read_normalization(tree)}
        metadata = {'normalization': read_normalization(tree)} if include_metadata else {}
        weighted_muons = read_muon_weight_sum(tree)
        if weights_only:
            return 'successful', {'written': int(tree.GetEntries()),
                                  'weighted_muons': weighted_muons}
        tree.SetEstimate(int(tree.GetEntries()) + 1)
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
        return 'successful', {**metadata, 'written': int(tree.GetEntries()), 'materials': materials,
                              'weighted_muons': weighted_muons}
    finally:
        root_file.Close()


def eos_path(filename):
    """Convert a recorded EOS path or XRootD URL to a mounted EOS path."""
    path = urlsplit(filename).path if filename.startswith('root://') else filename
    if not path.startswith('/'):
        return None
    path = '/' + path.lstrip('/')
    return path if path.startswith('/eos/') else None


def print_stats(label, values, width=30):
    print(f'{label:<{width}} {sum(values):>15.8g} {statistics.mean(values):>15.8g} '
          f'{statistics.pstdev(values):>15.8g}')


def print_path_statistics(results):
    """Aggregate counts at matching thresholds and take the largest jump."""
    groups = {}
    for result in results:
        diagnostics = result['path_diagnostics']
        settings = (diagnostics['z_min_cm'], diagnostics['jump_threshold_cm'])
        groups.setdefault(settings, []).append(diagnostics)
    for (z_min, threshold), diagnostics in sorted(groups.items()):
        print(f'\nMuon path diagnostics: {len(diagnostics)} subjobs '
              f'(minimum z = {z_min:g} cm, POCA threshold = {threshold:g} cm).')
        print('Routes are counted before material navigation; starts below minimum z '
              'still have paths built.')
        print(f'{"Quantity":<50} {"Sum":>15} {"Mean/subjob":>15} {"SD/subjob":>15}')
        for key, label in (
                ('considered', 'Muons considered'),
                ('non_forward', 'Muons rejected: non-forward momentum'),
                ('invalid_measurements', 'Muons rejected: invalid measurements'),
                ('ubt_backward', 'Muons from UBT with backward extrapolation'),
                ('mc_field', 'Muons from MC start in field'),
                ('mc_field_free', 'Muons from MC start in field-free region'),
                ('beyond_zmax', 'Muons skipped: start z >= maximum z'),
                ('ubt_missing_ms', 'UBT backward paths missing MS'),
                ('before_zmin', f'Muons starting before z = {z_min:g} cm'),
                ('transitions', 'POCA transitions'),
                ('large_jumps', f'POCA transitions exceeding {threshold:g} cm'),
                ('muons_with_large_jumps', f'Muons with POCA jumps exceeding {threshold:g} cm')):
            print_stats(label, [item[key] for item in diagnostics], width=50)
        print('Maximum POCA transverse jump across subjobs: '
              f'{max(item["max_jump_cm"] for item in diagnostics):.8g} cm')
        print('Maximum POCA transverse jump per subjob (cm, job/subjob order): '
              + ', '.join(f'{item["max_jump_cm"]:.8g}' for item in diagnostics))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path,
                        help='Directory containing numbered Ganga master-job directories')
    parser.add_argument('--first-job', type=int, required=True,
                        help='First Ganga master job ID to check')
    parser.add_argument('--num-jobs', type=int, required=True,
                        help='Number of consecutive master jobs to check')
    parser.add_argument('--check-root', action='store_true',
                        help='Open and check all recorded ROOT outputs and read their statistics '
                             '(default: fast, local logs only)')
    parser.add_argument('--metadata', action='store_true',
                        help='Read ROOT headers and normalization metadata, without scanning event branches')
    args = parser.parse_args()
    base = args.path.expanduser()
    if not base.is_dir():
        parser.error(f'Not a directory: {base}')
    if args.first_job < 0 or args.num_jobs <= 0:
        parser.error('--first-job must be nonnegative and --num-jobs must be positive')
    statuses = Counter()
    job_problems = Counter()
    missing_stdout_subjobs = []
    problems = []
    results = []
    timings = []
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
        logs = subjob_logs(job_dir, include_unsplit=True)
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
                log_text = logfile.read_text(errors='replace')
                log_status, log_result = parse_stdout(log_text, output)
            except FileNotFoundError:
                log_status, log_result = 'missing stdout', None
                missing_stdout_subjobs.append(label)
            except OSError as error:
                log_status, log_result = f'unreadable stdout ({error})', None
            root_status, root_result = None, None
            if args.check_root or args.metadata:
                # Failed/incomplete worker logs can still have a transferred file.
                # Check it as well so corrupt outputs are not silently omitted.
                for filename in outputs:
                    if args.metadata:
                        root_status, root_result = root_statistics(
                            filename, metadata_only=not args.check_root, include_metadata=args.check_root)
                    else:
                        root_status, root_result = root_statistics(filename)
                    if root_result is None:
                        problems.append(f'{label}: {root_status}: {filename}')
            if log_result is None or ((args.check_root or args.metadata) and root_result is None):
                # Count each excluded subjob once; retain additional ROOT
                # diagnostics in the detailed problems list below.
                status = log_status if log_result is None else root_status
                statuses[status] += 1
                if log_result is None and log_status != 'missing stdout':
                    problems.append(f'{label}: {log_status}')
                if not outputs:
                    excluded_without_output += 1
                    if log_status != 'missing stdout':
                        problems.append(f'{label}: no recorded ROOT output path for rm')
                excluded_outputs.extend(outputs)
                continue
            statuses['successful'] += 1
            if args.check_root or args.metadata:
                log_result.update(root_result)
            results.append(log_result)
            timings.append(read_job_timing(logfile, log_text))
            included_outputs.add(eos_path(output) or output)

    print(f'Jobs {args.first_job}–{args.first_job + args.num_jobs - 1}')
    print(f'Subjobs discovered: {discovered_subjobs}')
    success_rate = (f'{100 * len(results) / discovered_subjobs:.2f}%'
                    if discovered_subjobs else 'undefined')
    success_label = ('Successful subjobs with valid ROOT output' if args.check_root
                     else 'Successful subjobs according to logs')
    print(f'{success_label}: {len(results)}/{discovered_subjobs} '
          f'({success_rate})')
    if args.metadata and not args.check_root:
        print('Metadata mode: ROOT headers checked; event baskets not validated.')
    elif not args.check_root:
        print('Fast mode: ROOT outputs not checked; use --check-root for validation and ROOT statistics.')
    print(f'Excluded subjobs: {discovered_subjobs - len(results)} '
          '(one primary reason per subjob below)')
    for status, count in sorted(statuses.items()):
        if status != 'successful':
            print(f'  {status}: {count}')
    if missing_stdout_subjobs:
        print('Jobs/subjobs with missing stdout: ' + ', '.join(missing_stdout_subjobs))
    print(f'Excluded subjobs without a recorded ROOT output path: {excluded_without_output}')
    for status, count in sorted(job_problems.items()):
        print(f'  Job-directory problem — {status}: {count}')
    if results:
        if args.check_root:
            print('\nStatistics include only successful subjobs with valid MuonDIS output.')
            print(f'All weighted DIS counts scaled by {WDIS_SCALE:.8g} (0.006 * 1e-27 × 6.02214076e23).')
            print('Weighted muons = sum(muon_MCTracks.fW[0]); no DIS scaling applied.')
        else:
            print('\nStatistics include only subjobs with successful logs and a recorded output path.')
        print('SD = population standard deviation of per-subjob counts about their mean.')
        print(f'{"Quantity":<30} {"Sum":>15} {"Mean/subjob":>15} {"SD/subjob":>15}')
        for key, label in (('processed', 'Input entries processed'),
                           ('written', 'MuonDIS entries written'),
                           ('weighted_muons', 'Muons written weighted'),
                           ('mu_plus', 'Muons mu+'), ('mu_minus', 'Muons mu-'),
                           ('skipped_events', 'Skipped malformed/non-muon'),
                           ('skipped_low_p', 'Skipped low-p muons'),
                           ('no_hits', 'Counted no detector hits')):
            if (key == 'weighted_muons' and not args.check_root) or (key == 'written' and not (args.check_root or args.metadata)):
                continue
            print_stats(label, [result[key] for result in results])
    if results and args.check_root:
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
    if results:
        print_path_statistics(results)
        print_extra_statistics(results)
        print_job_timing(timings)
        if args.metadata:
            try:
                print_normalization_statistics(results)
            except ValueError as error:
                problems.append(str(error))
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
    if problems or statuses['missing stdout']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
