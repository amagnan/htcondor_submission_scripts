#!/usr/bin/env python3
"""Print muDIS_normalization records using uproot; no ROOT or FairShip required."""

import argparse
import math
import sys


def find_named(collection, name):
    return next((item for item in collection
                 if item.member('fName', none_if_missing=True) == name), None)


def read_records(tree):
    info = tree.member('fUserInfo', none_if_missing=True)
    metadata = find_named(info, 'muDIS_normalization') if info is not None else None
    if metadata is None or metadata.classname != 'TList':
        raise ValueError('Missing muDIS_normalization collection in tree user information')
    version = find_named(metadata, 'version')
    if version is None or int(version.member('fVal')) != 1:
        raise ValueError('Unsupported normalization metadata version')
    rows = []
    for record in metadata:
        if record.classname != 'TList':
            continue
        identity = str(record.member('fName'))
        fields = {name: find_named(record, name) for name in ('stage', 'input_muons', 'sum_weights')}
        missing = [name for name, value in fields.items() if value is None]
        if missing:
            raise ValueError(f'Record {identity} is missing: {", ".join(missing)}')
        rows.append((identity, str(fields['stage'].member('fTitle')),
                     str(int(fields['input_muons'].member('fVal'))),
                     format(float(fields['sum_weights'].member('fVal')), '.17g')))
    return rows


def print_table(rows):
    stages = {}
    for _, stage, input_muons, sum_weights in rows:
        stages.setdefault(stage, []).append((int(input_muons), float(sum_weights)))
    totals = [('TOTAL', stage, str(sum(count for count, _ in values)),
               format(math.fsum(weight for _, weight in values), '.17g'))
              for stage, values in stages.items()]
    headers = ('record', 'stage', 'input_muons', 'sum_weights')
    widths = [max(len(row[column]) for row in [headers, *rows, *totals]) for column in range(len(headers))]

    def line(row):
        return '  '.join(value.ljust(width) if column < 2 else value.rjust(width)
                         for column, (value, width) in enumerate(zip(row, widths)))

    print(line(headers))
    print('  '.join('-' * width for width in widths))
    for row in rows:
        print(line(row))
    if totals:
        print('  '.join('-' * width for width in widths))
        for row in totals:
            print(line(row))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', help='ROOT file containing normalization metadata')
    parser.add_argument('--tree', default='cbmsim', help='Tree name (default: cbmsim; use MuonDIS for preparation outputs)')
    args = parser.parse_args(argv)
    try:
        import uproot
    except ImportError:
        parser.exit(1, 'Install uproot first: python3 -m pip install uproot\n')
    try:
        # uproot's default minimal metadata mode omits the tree's fUserInfo.
        with uproot.open(args.file, object_cache=None, array_cache=None, minimal_ttree_metadata=False) as source:
            tree = source[args.tree]
            if tree.classname != 'TTree':
                raise ValueError(f'Not a TTree: {args.tree}')
            print_table(read_records(tree))
    except (OSError, KeyError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
