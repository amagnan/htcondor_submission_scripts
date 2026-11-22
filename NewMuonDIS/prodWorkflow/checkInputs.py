#!/usr/bin/env python3
"""Inspect disgen input tree headers without planning jobs or writing state."""

import argparse
from collections import Counter
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True

if __package__:
    from .config import load
else:
    from config import load


def inspect(path, root):
    source = None
    try:
        source = root.TFile.Open(str(path), 'READ')
        if not source or source.IsZombie():
            return (), 'unreadable'
        trees = []
        for name in sorted({key.GetName() for key in source.GetListOfKeys()}):
            key = source.GetKey(name)
            cls = root.TClass.GetClass(key.GetClassName())
            if cls and cls.InheritsFrom('TTree'):
                tree = source.Get(name)
                trees.append((name, int(tree.GetEntries())))
        return tuple(trees), 'ok'
    except (OSError, RuntimeError, ReferenceError):
        return (), 'unreadable'
    finally:
        if source:
            source.Close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('yaml_file', type=Path, help='disgen step YAML')
    parser.add_argument('--checkInputs', action='store_true', help='Inspect inputs and exit (also the standalone default)')
    parser.add_argument('--root-env', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    settings = load(args.yaml_file)
    if settings['step'] != 'disgen':
        parser.error('Input discovery is only supported for disgen')
    directory = Path(settings['input_dir'])
    if not directory.is_dir():
        parser.error(f'Not an input directory: {directory}')
    try:
        import ROOT
    except ImportError:
        if args.root_env:
            parser.error('PyROOT is unavailable in the configured FairShip environment')
        return subprocess.run([
            'bash', '/cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs', settings['fs_install'],
            'python3', '-B', str(Path(__file__).resolve()), str(args.yaml_file.resolve()), '--root-env',
        ]).returncode
    # Match planning.py discovery, including its exclusions; do not apply job/range limits.
    paths = [path for path in sorted(directory.rglob(settings['input_pattern']))
             if not path.name.endswith('_rec.root') and '_backup' not in path.stem and path.is_file()]
    counts = Counter()
    problems = []
    for path in paths:
        trees, status = inspect(path, ROOT)
        entries = dict(trees).get('cbmsim')
        if status == 'ok' and entries is None:
            status = 'missing cbmsim'
        elif status == 'ok' and entries == 0:
            status = 'empty cbmsim'
        counts[(trees, status)] += 1
        if status != 'ok':
            problems.append((path.relative_to(directory), status))
    rows = [('Files', 'cbmsim', 'Tree(s)', 'Entries/file', 'Status')]
    for (trees, status), count in sorted(counts.items()):
        rows.append((str(count), 'yes' if 'cbmsim' in dict(trees) else 'no',
                     ', '.join(name for name, _ in trees) or '-',
                     ', '.join(str(entries) for _, entries in trees) or '-', status))
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    print(f'Input directory: {directory}\nPattern: {settings["input_pattern"]}')
    for row in rows:
        print('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    print(f'Total: {len(paths)} files; {len(problems)} unusable inputs')
    for path, status in problems:
        print(f'  {path}: {status}')
    return int(not paths or bool(problems))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit(str(error))
