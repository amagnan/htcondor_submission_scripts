#!/usr/bin/env python3
"""Compare cbmsim/MCTrack[0] input muons using split branches (no PyROOT)."""

import argparse
import json
from pathlib import Path

import awkward as ak
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LogNorm
import numpy as np
import uproot


FIELDS = dict(pdg='fPdgCode', mother='fMotherId', px='fPx', py='fPy', pz='fPz',
              x='fStartX', y='fStartY', z='fStartZ', w='fW')
LABELS = dict(p='p [GeV/c]', pt='pT [GeV/c]', px='px [GeV/c]', py='py [GeV/c]',
              pz='pz [GeV/c]', x='Start x [cm]', y='Start y [cm]', z='Start z [cm]',
              r='Start radius [cm]', tx='px/pz [mrad]', ty='py/pz [mrad]',
              theta='Polar angle [mrad]', w='Input weight', pdg='PDG code')
COLORS = ['#0072B2', '#D55E00']


def read_sample(path):
    print(f'Opening {path}', flush=True)
    chunks = {key: [] for key in FIELDS}
    empty = scanned = 0
    with uproot.open(path, array_cache=None,
                     handler=uproot.source.file.MultithreadedFileSource) as source:
        tree = source['cbmsim']
        entries = tree.num_entries
        metadata = str(source['FileSummary']) if 'FileSummary' in source else None
        expressions = [f'MCTrack.{field}' for field in FIELDS.values()]
        for arrays in tree.iterate(expressions, step_size=25000, library='ak', how=dict):
            present = ak.num(arrays[expressions[0]]) > 0
            empty += int(ak.sum(~present))
            for key, branch in zip(FIELDS, expressions):
                chunks[key].append(ak.to_numpy(ak.firsts(arrays[branch][present])))
            scanned += len(present)
            print(f'  Read {scanned}/{entries} entries', flush=True)
    assert scanned == entries
    data = {key: np.concatenate(values).astype(float) for key, values in chunks.items()}
    primary_muon = (np.abs(data['pdg']) == 13) & (data['mother'] < 0)
    counts = dict(entries=entries, empty_tracks=empty, non_muon_first_tracks=int(
        np.sum(np.abs(data['pdg']) != 13)), nonprimary_first_tracks=int(np.sum(data['mother'] >= 0)),
        selected=int(np.sum(primary_muon)))
    data = {key: values[primary_muon] for key, values in data.items()}
    if not len(data['w']) or any(not np.all(np.isfinite(v)) for v in data.values()):
        raise ValueError('Empty sample or non-finite input values')
    if np.any(data['w'] < 0) or np.sum(data['w']) <= 0:
        raise ValueError('Expected nonnegative weights with positive total')
    data['pt'] = np.hypot(data['px'], data['py'])
    data['p'] = np.hypot(data['pt'], data['pz'])
    data['r'] = np.hypot(data['x'], data['y'])
    data['theta'] = 1000 * np.arctan2(data['pt'], data['pz'])
    for key, component in [('tx', 'px'), ('ty', 'py')]:
        data[key] = np.divide(1000 * data[component], data['pz'],
                              out=np.full_like(data['pz'], np.nan), where=data['pz'] != 0)
    counts['zero_pz'] = int(np.sum(data['pz'] == 0))
    counts['nonforward_pz'] = int(np.sum(data['pz'] <= 0))
    return data, dict(path=str(path), metadata=metadata, **counts)


def quantiles(values, weights):
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    indices = np.searchsorted(cumulative, np.array([.05, .5, .95]) * cumulative[-1])
    return values[order][indices].tolist()


def statistics(data, info):
    weights = data['w']
    info.update(sum_w=float(weights.sum()), sum_w2=float(np.sum(weights ** 2)),
                effective_entries=float(weights.sum() ** 2 / np.sum(weights ** 2)),
                weight_min=float(weights.min()), weight_max=float(weights.max()),
                zero_weights=int(np.sum(weights == 0)))
    info['variables'] = {}
    for key in LABELS:
        good = np.isfinite(data[key])
        values, w = data[key][good], weights[good]
        info['variables'][key] = dict(min=float(values.min()), max=float(values.max()),
            mean=float(values.mean()), rms=float(values.std()),
            quantiles_05_50_95=quantiles(values, np.ones(len(values))),
            weighted_mean=float(np.average(values, weights=w)),
            weighted_rms=float(np.sqrt(np.average((values - np.average(values, weights=w)) ** 2, weights=w))),
            weighted_quantiles_05_50_95=quantiles(values, w))
    info['fractions'] = {}
    masks = {'mu_minus': data['pdg'] == 13, 'mu_plus': data['pdg'] == -13}
    masks.update({f'p_below_{cut}_GeV': data['p'] < cut for cut in (2, 5, 10, 20, 50, 100)})
    for key, mask in masks.items():
        info['fractions'][key] = dict(count=int(mask.sum()), fraction=float(mask.mean()),
                                      weighted_fraction=float(weights[mask].sum() / weights.sum()))
    return info


def edges_for(samples, key):
    values = np.concatenate([d[key][np.isfinite(d[key])] for d in samples])
    lo, hi = float(values.min()), float(values.max())
    if key == 'pdg':
        return np.array([-14, -12, 12, 14], dtype=float)
    padding = .02 * (hi - lo) if hi != lo else max(abs(lo) * .01, .5)
    return np.linspace(lo - padding, hi + padding, 61)


def histogram(data, key, edges, weighted):
    w = data['w'] if weighted else np.ones(len(data[key]))
    good = np.isfinite(data[key])
    counts = np.histogram(data[key][good], bins=edges, weights=w[good])[0]
    variance = np.histogram(data[key][good], bins=edges, weights=w[good] ** 2)[0]
    if 'event' in data:
        # Hits from one muon are correlated: square each event's bin contribution.
        bins = np.clip(np.searchsorted(edges, data[key][good], side='right') - 1,
                       0, len(edges) - 2)
        groups, inverse = np.unique(data['event'][good].astype(np.int64) * (len(edges) - 1)
                                    + bins, return_inverse=True)
        contributions = np.bincount(inverse, weights=w[good])
        variance = np.bincount(groups % (len(edges) - 1), weights=contributions ** 2,
                               minlength=len(edges) - 1)
    assert np.isclose(counts.sum(), w[good].sum(), rtol=1e-9)
    total = w.sum()
    return counts / total, variance / total ** 2


def make_plots(samples, names, out, summaries, title='Input muons',
               detail='native start positions; all selected entries'):
    histogram_rows = []
    with PdfPages(out / 'comparison.pdf') as pdf:
        for weighted in (False, True):
            mode = 'weighted' if weighted else 'unweighted'
            for page, keys in [('momentum', ['p', 'pt', 'px', 'py', 'pz', 'theta']),
                               ('position_direction', ['x', 'y', 'z', 'r', 'tx', 'ty'])]:
                fig = plt.figure(figsize=(15, 11), constrained_layout=True)
                outer = fig.add_gridspec(2, 3)
                for cell, key in zip(outer, keys):
                    sub = cell.subgridspec(2, 1, height_ratios=[3, 1], hspace=.04)
                    ax = fig.add_subplot(sub[0])
                    ratio_ax = fig.add_subplot(sub[1], sharex=ax)
                    edges = edges_for(samples, key)
                    histograms = [histogram(d, key, edges, weighted) for d in samples]
                    for index, ((h, v), name, color) in enumerate(zip(histograms, names, COLORS)):
                        ax.stairs(h, edges, label=name, color=color, linewidth=1.5)
                        for left, right, value, variance in zip(edges[:-1], edges[1:], h, v):
                            histogram_rows.append((mode, key, name, left, right, value, variance))
                    a, va = histograms[0]
                    b, vb = histograms[1]
                    valid = (a > 0) & (b > 0)
                    ratio = b[valid] / a[valid]
                    error = ratio * np.sqrt(va[valid] / a[valid] ** 2 + vb[valid] / b[valid] ** 2)
                    ratio_ax.errorbar(((edges[:-1] + edges[1:]) / 2)[valid], ratio,
                                      yerr=error, fmt='.', color=COLORS[1], markersize=3)
                    ratio_ax.axhline(1, color='gray', linewidth=1)
                    ratio_ax.set_ylim(0, 3)
                    ratio_ax.set_ylabel('B / A')
                    ratio_ax.set_xlabel(LABELS[key])
                    ax.set_yscale('log')
                    ax.set_ylabel('Fraction / bin')
                    ax.tick_params(labelbottom=False)
                    ax.grid(alpha=.15)
                    if key == keys[0]:
                        ax.legend(fontsize=8)
                fig.suptitle(f'{title}: {page.replace("_", " & ")} — {mode}, unit-normalized\n'
                             f'A = MS5; B = v2 2018; {detail}', fontsize=14)
                fig.supxlabel('Ratios shown for bins populated in both samples; display range 0–3. '
                              'Errors: squared event contributions, fixed normalization; no exposure normalization.', fontsize=9)
                pdf.savefig(fig)
                fig.savefig(out / f'{mode}_{page}.png', dpi=140)
                plt.close(fig)
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.5), constrained_layout=True)
        for data, name, color, info in zip(samples, names, COLORS, summaries):
            w = data['w']
            axes[0].stairs(np.histogram(w, bins=edges_for(samples, 'w'))[0] / len(w),
                           edges_for(samples, 'w'), color=color, label=name)
            for ax, field, ylabel in [(axes[1], 'fraction', 'Unweighted fraction'),
                                       (axes[2], 'weighted_fraction', 'Weighted fraction')]:
                ax.plot([0, 1], [info['fractions'][charge][field] for charge in ('mu_minus', 'mu_plus')],
                        'o-', color=color, label=name)
                ax.set_xticks([0, 1])
                ax.set_xticklabels(['mu− (PDG 13)', 'mu+ (PDG −13)'])
                ax.set_ylabel(ylabel)
                ax.set_ylim(0, 1)
        axes[0].set_xlabel('Input weight')
        axes[0].set_ylabel('Unweighted fraction / bin')
        axes[0].set_yscale('log')
        axes[0].legend()
        fig.suptitle(f'{title}: input weights and muon charge composition')
        pdf.savefig(fig)
        fig.savefig(out / 'weights_charge.png', dpi=140)
        plt.close(fig)
        for x, y, title in [('x', 'y', 'start_xy'), ('p', 'pt', 'p_pt')]:
            xedges, yedges = edges_for(samples, x), edges_for(samples, y)
            histograms = [np.histogram2d(d[x], d[y], bins=[xedges, yedges], weights=d['w'])[0]
                          / d['w'].sum() for d in samples]
            positive = np.concatenate([h[h > 0] for h in histograms])
            norm = LogNorm(vmin=positive.min(), vmax=positive.max())
            fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
            for ax, h, name in zip(axes, histograms, names):
                mesh = ax.pcolormesh(xedges, yedges, np.ma.masked_equal(h.T, 0), norm=norm, cmap='viridis')
                ax.set(xlabel=LABELS[x], ylabel=LABELS[y], title=name)
            fig.colorbar(mesh, ax=axes, label='Weighted fraction / bin')
            fig.suptitle(f'{title}, weighted unit-normalized distributions (common bins and color scale)')
            pdf.savefig(fig)
            fig.savefig(out / f'weighted_{title}.png', dpi=140)
            plt.close(fig)
    import csv
    with (out / 'histograms.csv').open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(['mode', 'variable', 'sample', 'low', 'high', 'fraction', 'variance_fixed_norm'])
        writer.writerows(histogram_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file_a', type=Path)
    parser.add_argument('file_b', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    names = ['A: MS5', 'B: v2 2018']
    samples, summaries = [], []
    for path, name in zip([args.file_a, args.file_b], names):
        data, info = read_sample(path)
        info['label'] = name
        samples.append(data)
        summaries.append(statistics(data, info))
        print(json.dumps(summaries[-1], indent=2), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(summaries, indent=2) + '\n')
    print('Rendering comparison plots', flush=True)
    make_plots(samples, names, args.output, summaries)
    print(f'Finished: {args.output / "comparison.pdf"}', flush=True)


if __name__ == '__main__':
    main()
