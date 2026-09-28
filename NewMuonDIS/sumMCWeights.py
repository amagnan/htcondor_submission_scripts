#!/usr/bin/env python3
"""Recursively sum MCTrack[0].fW without ROOT or FairShip class dictionaries.

Requires uproot, awkward and numpy. Reads only the split weight branch in
chunks; empty track collections contribute zero and are reported separately.
"""

import argparse
import math
from pathlib import Path
import sys


def sum_file(path, tree_name, branch_name, step_size):
    import awkward as ak
    import numpy as np
    import uproot

    with uproot.open(path, array_cache=None,
                     handler=uproot.source.file.MultithreadedFileSource) as root_file:
        tree = root_file[tree_name]
        entries = tree.num_entries
        print(f'{path}: entries={entries}', flush=True)
        tree[branch_name]  # Check that the split branch exists, even in an empty tree.
        partial_sums = []
        empty = 0
        count = 0
        for (weights,) in tree.iterate(expressions=[branch_name],
                                      step_size=step_size, library='ak', how=tuple):
            first = ak.firsts(weights, axis=1)
            empty += int(ak.sum(ak.is_none(first)))
            values = ak.to_numpy(ak.fill_none(first, 0))
            if not np.all(np.isfinite(values)):
                raise ValueError('non-finite first-track weight')
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
    parser.add_argument('--tree', default='cbmsim', help='Tree name (default: cbmsim)')
    parser.add_argument('--branch', default='MCTrack.fW',
                        help='Split weight branch (default: MCTrack.fW)')
    parser.add_argument('--step-size', default='100 MB',
                        help='Approximate input array size per chunk (default: 100 MB)')
    args = parser.parse_args()
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

    files = sorted(path for path in directory.rglob('*.root')
                   if 'filtered' not in path.name)
    selected = files if args.test is None else files[:args.test]
    print(f'Files found={len(files)}, selected={len(selected)}', flush=True)
    total_entries = total_empty = processed = failed = 0
    sums = []
    for path in selected:
        try:
            entries, weight_sum, empty = sum_file(
                path, args.tree, args.branch, args.step_size)
        except Exception as error:
            #print(f'ERROR {path}: {error}', file=sys.stderr, flush=True)
            failed += 1
            continue
        print(f'  sum(MCTrack[0].fW)={weight_sum:.17g}, empty_tracks={empty}', flush=True)
        total_entries += entries
        total_empty += empty
        sums.append(weight_sum)
        processed += 1
    print(f'TOTAL: processed={processed}/{len(files)} found, '
          f'selected={len(selected)}, failed={failed}')
    print(f'  raw_entries={total_entries}, sum(MCTrack[0].fW)={math.fsum(sums):.17g}, '
          f'empty_tracks={total_empty}')
    print('  Totals include successfully processed files only.')
    return 1 if failed or not files else 0


if __name__ == '__main__':
    sys.exit(main())
