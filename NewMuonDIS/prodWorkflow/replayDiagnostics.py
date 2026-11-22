"""Replay path-length checks using only ROOT headers and scalar baskets."""

import math


MATERIALS = ('MS', 'UBT', 'SBTsens', 'SBTfr', 'SSTsens', 'SSTfr', 'HE', 'AIR', 'HeBalloon', 'REST')
PATH_BRANCHES = ('muon_path_length', *(f'muon_path_length_{name}' for name in MATERIALS))


def path_schema(tree, analysis=False):
    """Legacy replay has no lengths; a partially written set is never valid."""
    branches = (('muon_path_length_total', *PATH_BRANCHES[1:]) if analysis else PATH_BRANCHES)
    present = [name for name in branches if tree.GetBranch(name)]
    if (present or analysis) and len(present) != len(branches):
        raise ValueError('Incomplete muon path-length branches: ' + ', '.join(name for name in branches if name not in present))
    for name in present:
        branch = tree.GetBranch(name)
        leaves = branch.GetListOfLeaves()
        leaf = branch.GetLeaf(name)
        if (branch.GetClassName() or len(leaves) != 1 or not leaf
                or leaf.GetTypeName() != 'Double_t' or leaf.GetLenStatic() != 1 or leaf.GetLeafCount()):
            raise ValueError(f'Expected scalar Double_t branch {name}')
    return present


def path_statistics(tree, analysis=False, scan=False):
    branches = path_schema(tree, analysis)
    result = dict(path_length_branches=branches)
    if not scan or not branches:
        return result
    entries = int(tree.GetEntries())
    totals = {}
    chunk = 100000
    tree.SetEstimate(chunk + 1)
    for name in branches:
        available, total, maximum = 0, 0., 0.
        for first in range(0, entries, chunk):
            count = min(chunk, entries - first)
            if tree.Draw(name, '', 'goff', count, first) != count:
                raise ValueError(f'Cannot read {name}')
            values = tree.GetV1()
            for index in range(count):
                value = float(values[index])
                if analysis and math.isnan(value):
                    continue
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f'Invalid {name} at entry {first + index}: {value}')
                available += 1
                total += value
                maximum = max(maximum, value)
        if not math.isfinite(total):
            raise ValueError(f'Non-finite path-length sum for {name}')
        label = 'total' if name in ('muon_path_length', 'muon_path_length_total') else name.removeprefix('muon_path_length_')
        totals[label] = dict(available=available, missing=entries - available, sum_cm=total, max_cm=maximum)
    result['path_lengths'] = totals
    return result


def print_path_statistics(results):
    checked = [item for item in results if 'path_length_branches' in item]
    if not checked:
        return
    present = sum(bool(item['path_length_branches']) for item in checked)
    print(f'Muon path-length branches: {present}/{len(checked)} outputs; '
          f'{len(checked) - present} legacy outputs without lengths.')
    scanned = [item['path_lengths'] for item in checked if 'path_lengths' in item]
    if not scanned:
        return
    print('Path lengths in cm, per replay event (repeated for each DIS from the same muon); '
          'unweighted, not preparation-muon totals. Total excludes REST; MS is the restricted sampling path.')
    print(f'{"Path":<12} {"Available":>12} {"Missing":>12} {"Mean (cm)":>15} {"Max (cm)":>15}')
    for material in ('total', *MATERIALS):
        values = [item[material] for item in scanned]
        available = sum(item['available'] for item in values)
        missing = sum(item['missing'] for item in values)
        mean = f'{sum(item["sum_cm"] for item in values) / available:.8g}' if available else 'undefined'
        maximum = f'{max(item["max_cm"] for item in values):.8g}' if available else 'undefined'
        print(f'{material:<12} {available:>12} {missing:>12} {mean:>15} {maximum:>15}')
