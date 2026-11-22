#!/usr/bin/env python3
"""Compare primary-muon UBT and Tr1 hits, using the input comparison plot helpers."""

import argparse
import json
from pathlib import Path

import awkward as ak
import numpy as np
import uproot

import compareInputMuons as comparison


DETECTORS = {'UBT': 'UpstreamTaggerPoint', 'Tr1': 'strawtubesPoint'}
FIELDS = dict(pdg='fPdgCode', px='fPx', py='fPy', pz='fPz', x='fX', y='fY', z='fZ',
              time='fTime', detector_id='fDetectorID', track_id='fTrackID')


def derived(data):
    data['pt'] = np.hypot(data['px'], data['py'])
    data['p'] = np.hypot(data['pt'], data['pz'])
    data['r'] = np.hypot(data['x'], data['y'])
    data['theta'] = 1000 * np.arctan2(data['pt'], data['pz'])
    for key, component in [('tx', 'px'), ('ty', 'py')]:
        data[key] = np.divide(1000 * data[component], data['pz'],
                              out=np.full_like(data['pz'], np.nan), where=data['pz'] != 0)
    return data


def read_hits(path, ubt_scoring_plane=None):
    print(f'Opening {path}', flush=True)
    detectors = dict(DETECTORS)
    if ubt_scoring_plane is not None:
        detectors['UBT'] = 'UpstreamTaggerScoringPoint'
    chunks = {name: {key: [] for key in (*FIELDS, 'w', 'event')} for name in DETECTORS}
    multiplicities = {name: [] for name in DETECTORS}
    input_weights = []
    offset = 0
    primary_fields = ['MCTrack.fPdgCode', 'MCTrack.fMotherId', 'MCTrack.fW']
    expressions = primary_fields + [f'{coll}.{field}' for coll in detectors.values()
                                    for field in FIELDS.values()]
    with uproot.open(path, array_cache=None,
                     handler=uproot.source.file.MultithreadedFileSource) as source:
        tree = source['cbmsim']
        for arrays in tree.iterate(expressions, step_size=25000, library='ak', how=dict):
            pdg, mother, weights = [ak.to_numpy(ak.fill_none(ak.firsts(arrays[k]), 0))
                                    for k in primary_fields]
            primary = (np.abs(pdg) == 13) & (mother < 0)
            weights = np.where(primary, weights, 0.)
            input_weights.append(weights)
            events = np.arange(offset, offset + len(weights))
            for name, coll in detectors.items():
                track = arrays[f'{coll}.fTrackID']
                select = (track == 0) & primary
                if name == 'Tr1':
                    select = select & (arrays[f'{coll}.fDetectorID'] // 1000000 == 1)
                elif ubt_scoring_plane is not None:
                    select = select & (arrays[f'{coll}.fDetectorID'] == ubt_scoring_plane)
                selected_pdg = ak.flatten(arrays[f'{coll}.fPdgCode'][select])
                if not bool(ak.all(np.abs(selected_pdg) == 13)):
                    raise ValueError(f'Non-muon hit assigned to track 0 in {name}')
                multiplicities[name].append(ak.to_numpy(ak.sum(select, axis=1)))
                for key, field in FIELDS.items():
                    chunks[name][key].append(ak.to_numpy(ak.flatten(arrays[f'{coll}.{field}'][select])))
                for key, values in [('w', weights), ('event', events)]:
                    broadcast = ak.broadcast_arrays(values, track)[0]
                    chunks[name][key].append(ak.to_numpy(ak.flatten(broadcast[select])))
            offset += len(weights)
            print(f'  Read {offset}/{tree.num_entries} entries', flush=True)
        assert offset == tree.num_entries
    weights = np.concatenate(input_weights)
    result = {}
    for name in DETECTORS:
        data = {key: np.concatenate(values) for key, values in chunks[name].items()}
        if not len(data['w']) or any(not np.all(np.isfinite(v)) for v in data.values()):
            raise ValueError(f'Empty or non-finite hit data for {name}')
        counts = np.concatenate(multiplicities[name])
        reached = counts > 0
        order = np.lexsort((np.arange(len(data['w'])), data['time'], data['event']))
        _, first_indices = np.unique(data['event'][order], return_index=True)
        first = {key: values[order[first_indices]] for key, values in data.items()}
        data, first = derived(data), derived(first)
        info = dict(path=str(path), entries=offset, selected_input_muons=int(np.sum(weights > 0)),
                    hit_collection=detectors[name],
                    scoring_plane_id=ubt_scoring_plane if name == 'UBT' else None,
                    sum_input_w=float(weights.sum()), hits=len(data['w']),
                    muons_with_hits=int(reached.sum()), hit_fraction=float(reached.mean()),
                    sum_reaching_muon_w=float(weights[reached].sum()),
                    weighted_hit_fraction=float(weights[reached].sum() / weights.sum()),
                    mean_hits_per_reaching_muon=float(counts[reached].mean()),
                    max_hits_per_muon=int(counts.max()),
                    multiplicity_counts={str(int(k)): int(v) for k, v in zip(*np.unique(counts, return_counts=True))},
                    detector_id_min=int(data['detector_id'].min()),
                    detector_id_max=int(data['detector_id'].max()),
                    backwards_hits=int(np.sum(data['pz'] < 0)), zero_pz_hits=int(np.sum(data['pz'] == 0)))
        info['all_hits'] = comparison.statistics(data, {})
        info['all_hits']['effective_entries_event_clustered'] = float(
            np.sum(weights * counts) ** 2 / np.sum((weights * counts) ** 2))
        info['earliest_hit_per_muon'] = comparison.statistics(first, {})
        assert len(first['w']) == int(reached.sum())
        assert np.isclose(first['w'].sum(), weights[reached].sum())
        result[name] = (data, first, info)
        print(f'{name}: {info["hits"]} hits from {info["muons_with_hits"]} muons; '
              f'weighted hit fraction {info["weighted_hit_fraction"]:.6%}', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file_a', type=Path)
    parser.add_argument('file_b', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ubt-b-scoring-plane', type=int, choices=(1, 2),
                        help='Use this UpstreamTaggerScoringPoint detector ID for sample B UBT')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    samples = [read_hits(args.file_a), read_hits(args.file_b, args.ubt_b_scoring_plane)]
    summary = {name: [sample[name][2] for sample in samples] for name in DETECTORS}
    (args.output / 'hit_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    for key in ('x', 'y', 'z', 'r'):
        comparison.LABELS[key] = comparison.LABELS[key].replace('Start', 'Hit')
    for name in DETECTORS:
        for mode, index in [('all_hits', 0), ('earliest_hit_per_muon', 1)]:
            out = args.output / name / mode
            out.mkdir(parents=True, exist_ok=True)
            data = [sample[name][index] for sample in samples]
            infos = [sample[name][2][mode] for sample in samples]
            print(f'Rendering {name}: {mode}', flush=True)
            detail = ('all hits, trackID = 0' if index == 0 else
                      'earliest-time hit per muon, trackID = 0')
            if name == 'UBT' and args.ubt_b_scoring_plane is not None:
                detail += f'; B: scoring plane {args.ubt_b_scoring_plane}'
            comparison.make_plots(data, ['A: MS5', 'B: v2 2018'], out, infos,
                                  title=f'{name} primary-muon hits',
                                  detail=detail)
    print('Finished hit comparisons', flush=True)


if __name__ == '__main__':
    main()
