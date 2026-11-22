#!/usr/bin/env python3
"""Recursively sum first-track MC weights for one DIS production stage.

Requires uproot, awkward and numpy. Reads only the split weight branch in
chunks; empty track collections contribute zero and are reported separately.
No ROOT or FairShip class dictionaries are needed.

Example: python3 sumMCWeights.py /path/to/production --step disgen --test 2
disgen also accepts legacy muonDis_cudaMu_*_evtSTART_END.root filenames.
cuda selects Muons_*.root (converted_ntuple/w) and sim_*.root
(cbmsim/MCTrack.fW) inputs recursively.
Use --step cuda --cuda-filter 2026 to select relative paths containing 2026.
"""

import argparse
import math
from pathlib import Path
import re
import sys


STAGES = {
    'cuda': ('cbmsim', 'MCTrack.fW'),
    'disgen': ('MuonDIS', 'muon_MCTracks.fW'),
    'disfilter': ('MuonDIS', 'muon_MCTracks.fW'),
    'disreplay_gen': ('cbmsim', 'MCTrack.fW'),
    'disreplay_filter': ('cbmsim', 'MCTrack.fW'),
}


def production_files(directory, step, cuda_filter=None):
    files = []
    for path in directory.rglob('*.root'):
        if not path.is_file() or (step != 'cuda' and '_backup' in path.stem):
            continue
        if step == 'cuda':
            matches = path.name.startswith(('sim_', 'Muons_'))
            if cuda_filter is not None:
                matches = matches and cuda_filter in path.relative_to(directory).as_posix()
        elif step.startswith('disreplay'):
            matches = path.name.startswith('sim_') and path.stem.endswith('_' + step)
        else:
            if path.name.startswith(('sim_', 'geo_', 'params_')):
                continue
            matches = path.stem.endswith('_' + step)
            if step == 'disgen':
                matches = matches or re.fullmatch(
                    r'muonDis_cudaMu_.+_evt\d+_\d+\.root', path.name) is not None
        if matches:
            files.append(path)
    return sorted(files)


def sum_file(path, tree_name, branch_name, step_size, first_track=True):
    import awkward as ak
    import numpy as np
    import uproot

    with uproot.open(path, array_cache=None,
                     handler=uproot.source.file.MultithreadedFileSource) as root_file:
        tree = root_file[tree_name]
        entries = tree.num_entries
        print(f'{path}: entries={entries}', flush=True)
        tree[branch_name]  # Check that the weight branch exists, even in an empty tree.
        partial_sums = []
        empty = 0
        count = 0
        for (weights,) in tree.iterate(expressions=[branch_name],
                                      step_size=step_size, library='ak', how=tuple):
            first = ak.firsts(weights, axis=1) if first_track else weights
            empty += int(ak.sum(ak.is_none(first)))
            values = ak.to_numpy(ak.fill_none(first, 0))
            if not np.all(np.isfinite(values)):
                label = 'first-track weight' if first_track else 'weight'
                raise ValueError(f'non-finite {label}')
            partial_sums.append(float(np.sum(values, dtype=np.float64)))
            count += len(values)
        if count != entries:
            raise ValueError(f'read {count} weights for {entries} entries')
        return entries, math.fsum(partial_sums), empty


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_dir', type=Path)
    parser.add_argument('--test', '--max-files', type=int, metavar='N',
                        help='Process only the first N files (sorted by path)')
    parser.add_argument('--step', required=True, choices=STAGES,
                        help='Production stage; selects filenames, tree and weight branch')
    parser.add_argument('--cuda-filter', metavar='STRING',
                        help='With --step cuda, select relative input paths containing STRING (case-sensitive)')
    parser.add_argument('--step-size', default='100 MB',
                        help='Approximate input array size per chunk (default: 100 MB)')
    args = parser.parse_args()
    if args.cuda_filter is not None and args.step != 'cuda':
        parser.error('--cuda-filter requires --step cuda')
    directory = args.input_dir.expanduser()
    if not directory.is_dir():
        parser.error(f'Not a directory: {directory}')
    if args.test is not None and args.test < 1:
        parser.error('--test / --max-files must be positive')
    try:
        import uproot
        import awkward
        import numpy
    except ImportError:
        parser.error('Install dependencies with: python3 -m pip install uproot awkward numpy')

    tree_name, branch_name = STAGES[args.step]
    weight_label = 'weight' if args.step == 'cuda' else branch_name.replace('.fW', '[0].fW')
    files = production_files(directory, args.step, args.cuda_filter)
    selected = files if args.test is None else files[:args.test]
    if args.step == 'cuda':
        print('Stage=cuda, weights=converted_ntuple/w or cbmsim/MCTrack[0].fW', flush=True)
    else:
        print(f'Stage={args.step}, tree={tree_name}, branch={branch_name}', flush=True)
    print(f'Files found={len(files)}, selected={len(selected)}', flush=True)
    total_entries = total_empty = processed = failed = 0
    sums = []
    for path in selected:
        raw_cuda = args.step == 'cuda' and path.name.startswith('Muons_')
        tree_name, branch_name = ('converted_ntuple', 'w') if raw_cuda else STAGES[args.step]
        file_weight_label = branch_name.replace('.fW', '[0].fW')
        try:
            entries, weight_sum, empty = sum_file(
                path, tree_name, branch_name, args.step_size, first_track=not raw_cuda)
        except Exception as error:
            print(f'ERROR {path}: {error}', file=sys.stderr, flush=True)
            failed += 1
            continue
        print(f'  sum({file_weight_label})={weight_sum:.17g}, empty_tracks={empty}', flush=True)
        total_entries += entries
        total_empty += empty
        sums.append(weight_sum)
        processed += 1
    print(f'TOTAL: processed={processed}/{len(files)} found, '
          f'selected={len(selected)}, failed={failed}')
    print(f'  raw_entries={total_entries}, sum({weight_label})={math.fsum(sums):.17g}, '
          f'empty_tracks={total_empty}')
    print('  Totals include successfully processed files only.')
    return 1 if failed or not files else 0


if __name__ == '__main__':
    sys.exit(main())
