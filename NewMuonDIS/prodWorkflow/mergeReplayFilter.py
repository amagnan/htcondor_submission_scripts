#!/usr/bin/env python3
"""Merge filtered cbmsim events, normalization and acceptance diagnostics locally."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prodWorkflow.replayMerge import (ROOT_NAME, GEO_NAME, MANIFEST_NAME, DIAGNOSTICS_NAME,
                                      DIAGNOSTICS_MANIFEST, checksum)
from prodWorkflow.replayProduction import read_filter_manifest, complete_masters
from prodWorkflow.state import verify

DIAGNOSTIC_KEYS = ('replayFilterDiagnostics', 'replayFilterMuons', 'replayFilterPlots')
COUNT_KEYS = ('entries_available', 'events_processed', 'events_two_tracks', 'events_selected')


def native_diagnostics(root):
    if not hasattr(root, 'ReplayMergeDiagnostics'):
        print('Compiling C++ diagnostic merger', flush=True)
        source = Path(__file__).with_name('replayMergeDiagnostics.hxx').read_text()
        if not root.gInterpreter.Declare('#pragma cling optimize(2)\n' + source):
            raise RuntimeError('Cannot compile C++ diagnostic merger')
        print('C++ diagnostic merger ready', flush=True)
    return root.ReplayMergeDiagnostics


def native_call(root, function, *args):
    """Keep the merger's public validation/I/O exception types."""
    try:
        return function(*args)
    except root.std.invalid_argument as error:
        raise ValueError(str(error)) from error
    except root.std.runtime_error as error:
        raise OSError(str(error)) from error


def open_root(root, path):
    file = root.TFile.Open(str(path), 'READ')
    if not file or file.IsZombie() or file.TestBit(root.TFile.kRecovered):
        if file:
            file.Close()
        raise ValueError(f'Cannot read intact ROOT input: {path}')
    return file


def tree_schema(tree):
    if not tree or not tree.InheritsFrom('TTree'):
        raise ValueError('Missing ROOT tree')

    def branches(items):
        return [(branch.GetName(), branch.GetClassName(), branch.GetTitle(),
                 [(leaf.GetName(), leaf.GetTypeName(), leaf.GetLenStatic(),
                   leaf.GetLeafCount().GetName() if leaf.GetLeafCount() else None)
                  for leaf in branch.GetListOfLeaves()], branches(branch.GetListOfBranches()))
                for branch in items]
    return branches(tree.GetListOfBranches())


def serialized(root, obj):
    return str(root.TBufferJSON.ConvertToJSON(obj))


def normalization_tree(root, normalization):
    collection = root.TList()
    collection.SetName('muDIS_normalization')
    collection.SetOwner(True)
    root.SetOwnership(collection, False)
    for item in (root.TParameter(int)('version', 1),
                 root.TParameter(int)('preparation_complete', int(normalization['preparation_complete']))):
        root.SetOwnership(item, False)
        collection.Add(item)
    return collection


class Diagnostics:
    """Combine scalar trees and count histograms; derive efficiencies after summing."""

    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        self.native = native_diagnostics(root)
        self.writers, self.schemas, self.histograms, self.histogram_sets = {}, {}, {}, {}
        self.definition = None
        self.rows = dict.fromkeys(DIAGNOSTIC_KEYS[:2], 0)

    def writer(self, name, tree):
        schema = tree_schema(tree)
        if name in self.schemas and self.schemas[name] != schema:
            raise ValueError(f'Incompatible diagnostic tree schema: {name}')
        if name not in self.writers:
            self.writers[name] = native_call(self.root, self.native.ScalarMerger, tree, self.directory)
            self.schemas[name] = schema
        return self.writers[name]

    def add(self, source, record, file_offset, selected_start):
        definition = record['diagnostics']
        if definition.get('version') != 1 or (self.definition is not None and definition != self.definition):
            raise ValueError('Incompatible replayfilter diagnostic definitions')
        self.definition = definition
        for name in DIAGNOSTIC_KEYS[:2]:
            tree = source.Get(name)
            writer = self.writer(name, tree)
            entries = int(tree.GetEntries())
            print(f'Combining {name}: {entries} rows', flush=True)
            self.rows[name] += int(native_call(self.root, writer.add, tree, record['source_uuid'],
                                               record['source_entry_start'], file_offset, selected_start,
                                               record['events_processed'], record['events_selected'],
                                               record['events_two_tracks']))
        plots = source.GetDirectory('replayFilterPlots')
        if not plots:
            raise ValueError('Missing replayfilter diagnostic histograms')
        for sample, count in (('all', record['events_processed']), ('accepted', record['events_selected']),
                              ('rejected', record['events_processed'] - record['events_selected'])):
            histogram = plots.Get(f'ALL/muon_p_GeV_{sample}')
            if not histogram or histogram.GetEntries() != count:
                raise ValueError(f'Diagnostic histogram count differs: {sample}')
        self.add_histograms(plots)

    @staticmethod
    def histogram_schema(histogram):
        axes = [histogram.GetXaxis(), histogram.GetYaxis(), histogram.GetZaxis()][:histogram.GetDimension()]
        return (histogram.ClassName(), [[axis.GetBinLowEdge(i) for i in range(1, axis.GetNbins() + 2)]
                                        for axis in axes])

    def add_histograms(self, plots):
        for material in dict.fromkeys(key.GetName() for key in plots.GetListOfKeys()):
            directory = plots.GetDirectory(material)
            if not directory:
                raise ValueError(f'Unexpected object in diagnostic plots: {material}')
            names = set(key.GetName() for key in directory.GetListOfKeys())
            bases = {name[:-4] for name in names if name.endswith('_all')}
            if not bases or names != {base + '_' + sample for base in bases
                                      for sample in ('all', 'accepted', 'rejected', 'efficiency')}:
                raise ValueError(f'Incomplete diagnostic histogram set: {material}')
            if material in self.histogram_sets and self.histogram_sets[material] != names:
                raise ValueError(f'Incompatible diagnostic histogram set: {material}')
            self.histogram_sets[material] = names
            for name in sorted(names):
                if name.endswith('_efficiency'):
                    continue
                histogram = directory.Get(name)
                if not histogram or not histogram.InheritsFrom('TH1'):
                    raise ValueError(f'Missing diagnostic count histogram: {material}/{name}')
                key = material, name
                if key not in self.histograms:
                    self.histograms[key] = histogram.Clone()
                    self.histograms[key].SetDirectory(0)
                else:
                    combined = self.histograms[key]
                    if self.histogram_schema(combined) != self.histogram_schema(histogram):
                        raise ValueError(f'Incompatible histogram axes: {material}/{name}')
                    if not combined.Add(histogram):
                        raise OSError(f'Cannot add histogram: {material}/{name}')

    def write(self):
        self.directory.cd()
        for writer in self.writers.values():
            native_call(self.root, writer.write)
        plots = self.directory.mkdir('replayFilterPlots')
        for material, names in sorted(self.histogram_sets.items()):
            plots.mkdir(material).cd()
            for name in sorted(names):
                if name.endswith('_efficiency'):
                    base = name[:-11]
                    accepted = self.histograms[material, base + '_accepted']
                    total = self.histograms[material, base + '_all']
                    rejected = self.histograms[material, base + '_rejected']
                    if not (self.histogram_schema(total) == self.histogram_schema(accepted)
                            == self.histogram_schema(rejected)):
                        raise ValueError(f'Incompatible numerator/denominator axes: {material}/{base}')
                    native_call(self.root, self.native.validateHistogramCounts,
                                total, accepted, rejected, f'{material}/{base}')
                    histogram = accepted.Clone(name)
                    histogram.SetDirectory(0)
                    histogram.SetTitle(histogram.GetTitle().replace('accepted', 'efficiency').replace('Replay events', 'Accepted / all'))
                    if not histogram.Divide(accepted, total, 1, 1, 'B'):
                        raise OSError(f'Cannot calculate efficiency: {material}/{base}')
                else:
                    histogram = self.histograms[material, name]
                if histogram.Write() <= 0:
                    raise OSError(f'Cannot write diagnostic histogram: {material}/{name}')
        self.directory.cd()
        if self.root.TNamed('definition', json.dumps(self.definition)).Write() <= 0:
            raise OSError('Cannot write diagnostic definitions')
        return self.rows


def merge(root, plan, local):
    """Caller loads libShipMuDIS once. Publish the local bundle only when complete."""
    from analyseEvents import Geometry, read_normalization
    from MuDISGenerator_postProcessing import _copy_metadata

    local = Path(local)
    if local.exists():
        raise FileExistsError(f'Local replayfilter merge already exists: {local}')
    local.parent.mkdir(parents=True, exist_ok=True)
    inputs = plan['inputs']
    if not inputs or len({item['path'] for item in inputs}) != len(inputs):
        raise ValueError('Replayfilter merge requires distinct input files')
    records, schemas, uuids, geometries = [], [], set(), set()
    geometry_signature, expected_entries = None, 0
    for item in inputs:
        verify(item['files'])
        record = read_filter_manifest(item['manifest'])
        if (record['production']['test'] != plan['test'] or record['filter'].get('cross_section', 'stored') != plan['cross_section']
                or Path(item['path']).name != record['output']
                or Path(item['geometry']) != Path(item['path']).with_name(record['geometry'])):
            raise ValueError('Replayfilter input differs from merge plan')
        with open_root(root, item['path']) as source:
            tree = source.Get('cbmsim')
            schemas.append(tree_schema(tree))
            if schemas[-1] != schemas[0]:
                raise ValueError(f'Incompatible cbmsim schema: {item["path"]}')
            entries = int(tree.GetEntries())
            if (entries != item['entries'] or entries != record['filter']['events_selected']
                    or item['entry_start'] != expected_entries or item['entry_stop'] != expected_entries + entries):
                raise ValueError('Replayfilter entry count/offset differs from plan')
            expected_entries += entries
            uuid = str(source.GetUUID().AsString())
            if uuid in uuids or item['expected_uuid'] is not None and uuid != item['expected_uuid']:
                raise ValueError('Duplicate or changed replayfilter ROOT UUID')
            uuids.add(uuid)
            marker = source.Get('replayFilter')
            if not marker or json.loads(str(marker.GetTitle())) != record['filter']:
                raise ValueError('ROOT replayFilter metadata differs from worker manifest')
            if any(('diagnostics' in origin) != plan['diagnostics'] for origin in record['filter']['sources']):
                raise ValueError('Replayfilter diagnostic availability differs from plan')
        if item['geometry'] not in geometries:
            geometry = Geometry(root, item['geometry'])
            geometry.load(Path(item['path']))
            current = serialized(root, geometry.manager)
            if geometry_signature is not None and current != geometry_signature:
                raise ValueError('Replayfilter geometries differ')
            geometry_signature = current
            geometries.add(item['geometry'])
        records.append((Path(item['manifest']), record))
    complete_masters(records)
    if expected_entries != plan['entries']:
        raise ValueError('Merged entry count differs from plan')
    paths = [Path(item['path']) for item in inputs]
    normalization, usable = read_normalization(root, paths, 'cbmsim')
    if usable != paths or not normalization['preparation_complete']:
        raise ValueError('Replayfilter merge requires complete normalization for every input')
    summary = dict(version=1, kind='replayfilter_merge', plan_id=plan['id'],
                   cross_section=plan['cross_section'], scope=plan['scope'], inputs=[], sources=[],
                   diagnostics_enabled=plan['diagnostics'] and not plan.get('defer_diagnostics', False))
    if plan.get('defer_diagnostics'):
        summary['diagnostics_deferred'] = plan['diagnostics']
    summary.update({key: sum(record['filter'][key] for _, record in records) for key in COUNT_KEYS})
    with tempfile.TemporaryDirectory(prefix='.replayfilter-local-', dir=local.parent) as temporary:
        folder = Path(temporary)
        output = folder / ROOT_NAME
        root.TTree.SetMaxTreeSize(1 << 60)
        chain = root.TChain('cbmsim')
        for item in inputs:
            if chain.Add(item['path']) != 1:
                raise OSError(f'Cannot add replayfilter input: {item["path"]}')
        with open_root(root, paths[0]) as template, root.TFile.Open(str(output), 'RECREATE') as destination:
            destination.SetCompressionSettings(template.GetCompressionSettings())
            destination.cd()
            print(f'Copying cbmsim baskets: {len(inputs)} files, {plan["entries"]} entries', flush=True)
            selected = chain.CloneTree(-1, 'fast') if plan['entries'] else template.Get('cbmsim').CloneTree(0)
            if not selected or selected.GetEntries() != plan['entries']:
                raise OSError('Merged cbmsim entry count differs from input total')
            info = selected.GetUserInfo()
            for item in list(info):
                info.Remove(item)
            merged_normalization = normalization_tree(root, normalization)
            normalization_records = {}
            provenance = destination.mkdir('provenance')
            diagnostic = Diagnostics(root, destination.mkdir('diagnostics')) if summary['diagnostics_enabled'] else None
            for index, (item, (_, record)) in enumerate(zip(inputs, records)):
                print(f'Combining metadata {index + 1}/{len(inputs)}', flush=True)
                with open_root(root, item['path']) as source:
                    directory = provenance.mkdir(str(index))
                    _copy_metadata(source, directory, skip=('cbmsim', 'replay_sources', *DIAGNOSTIC_KEYS))
                    directory.cd()
                    user_info = source.Get('cbmsim').GetUserInfo()
                    if user_info.Write('tree_user_info', root.TObject.kSingleKey) <= 0:
                        raise OSError('Cannot preserve input tree user information')
                    if root.TNamed('worker_manifest', json.dumps(record)).Write() <= 0:
                        raise OSError('Cannot preserve worker manifest')
                    for entry in user_info.FindObject('muDIS_normalization'):
                        if not entry.InheritsFrom('TList'):
                            continue
                        identity, content = str(entry.GetName()), serialized(root, entry)
                        if identity in normalization_records:
                            if normalization_records[identity] != content:
                                raise ValueError(f'Conflicting normalization record: {identity}')
                        else:
                            normalization_records[identity] = content
                            clone = entry.Clone()
                            root.SetOwnership(clone, False)
                            merged_normalization.Add(clone)
                    summary['inputs'].append(dict(path=item['path'], uuid=str(source.GetUUID().AsString()),
                                                   entry_start=item['entry_start'], entry_stop=item['entry_stop'],
                                                   provenance=f'provenance/{index}'))
                    originals = directory.mkdir('replay_sources')
                    selected_start = 0
                    for origin_index, origin in enumerate(record['filter']['sources']):
                        origin_dir = source.GetDirectory(f'replay_sources/{origin_index}')
                        if not origin_dir:
                            raise ValueError('Missing original replay source metadata')
                        _copy_metadata(origin_dir, originals.mkdir(str(origin_index)), skip=DIAGNOSTIC_KEYS)
                        summary['sources'].append(dict(origin, filtered_input=index,
                                                        merged_entry_start=item['entry_start'] + selected_start,
                                                        merged_entry_stop=item['entry_start'] + selected_start + origin['events_selected']))
                        if diagnostic:
                            diagnostic.add(origin_dir, origin, item['entry_start'], selected_start)
                        selected_start += origin['events_selected']
            info.Add(merged_normalization)
            if diagnostic:
                summary['diagnostic_rows'] = diagnostic.write()
            destination.cd()
            if selected.Write('cbmsim', root.TObject.kOverwrite) <= 0:
                raise OSError('Cannot write merged cbmsim')
            if root.TNamed('replayFilter', json.dumps(summary)).Write() <= 0:
                raise OSError('Cannot write merged filter metadata')
            destination.Flush()
            if destination.TestBit(root.TFile.kWriteError):
                raise OSError('ROOT reported an error writing the merged file')
        # Reopen after close, independently of live input trees and buffers.
        with open_root(root, output) as file:
            if file.Get('cbmsim').GetEntries() != plan['entries'] or tree_schema(file.Get('cbmsim')) != schemas[0]:
                raise ValueError('Written merged tree schema/count differs from inputs')
            if json.loads(str(file.Get('replayFilter').GetTitle())) != summary:
                raise ValueError('Written merged provenance differs')
            if diagnostic:
                for name, count in summary['diagnostic_rows'].items():
                    if file.Get('diagnostics/' + name).GetEntries() != count:
                        raise ValueError('Written diagnostic tree count differs')
        combined, _ = read_normalization(root, [output], 'cbmsim')
        if combined != normalization:
            raise ValueError('Written normalization differs from the deduplicated input records')
        shutil.copyfile(inputs[0]['geometry'], folder / GEO_NAME)
        for item in inputs:
            verify(item['files'])
        receipt = dict(version=1, kind='replayfilter_merge', plan=plan, filter=summary,
                       normalization=normalization,
                       files={name: dict(size=(folder / name).stat().st_size, sha256=checksum(folder / name))
                              for name in (ROOT_NAME, GEO_NAME)})
        (folder / MANIFEST_NAME).write_text(json.dumps(receipt, indent=2, allow_nan=False) + '\n')
        if local.exists():
            raise FileExistsError(f'Local output appeared during merging: {local}')
        os.rename(folder, local)
    print(f'Validated local replayfilter merge: {local / ROOT_NAME}', flush=True)
    return receipt


def merge_diagnostics(root, plan, local):
    """Build a separate diagnostic bundle without copying or changing cbmsim."""
    if plan.get('kind') != 'replayfilter_diagnostics' or not plan.get('baseline_plan_id'):
        raise ValueError('Expected a deferred diagnostic plan linked to a baseline merge')
    local = Path(local)
    if local.exists():
        raise FileExistsError(f'Local diagnostic merge already exists: {local}')
    local.parent.mkdir(parents=True, exist_ok=True)
    summary = dict(version=1, kind=plan['kind'], plan_id=plan['id'],
                   baseline_plan_id=plan['baseline_plan_id'], entries=plan['entries'])
    with tempfile.TemporaryDirectory(prefix='.replayfilter-diagnostics-', dir=local.parent) as temporary:
        folder = Path(temporary)
        output = folder / DIAGNOSTICS_NAME
        root.TTree.SetMaxTreeSize(1 << 60)
        with root.TFile.Open(str(output), 'RECREATE') as destination:
            diagnostic = Diagnostics(root, destination.mkdir('diagnostics'))
            for index, item in enumerate(plan['inputs']):
                print(f'Combining diagnostics {index + 1}/{len(plan["inputs"])}', flush=True)
                verify(item['files'])
                record = read_filter_manifest(item['manifest'])
                with open_root(root, item['path']) as source:
                    if (item['expected_uuid'] is not None
                            and str(source.GetUUID().AsString()) != item['expected_uuid']):
                        raise ValueError('Replayfilter ROOT UUID changed')
                    marker = source.Get('replayFilter')
                    if (not marker or json.loads(str(marker.GetTitle())) != record['filter']
                            or source.Get('cbmsim').GetEntries() != item['entries']):
                        raise ValueError('Replayfilter input differs from diagnostic plan')
                    selected_start = 0
                    for origin_index, origin in enumerate(record['filter']['sources']):
                        origin_dir = source.GetDirectory(f'replay_sources/{origin_index}')
                        if not origin_dir:
                            raise ValueError('Missing original replay source metadata')
                        diagnostic.add(origin_dir, origin, item['entry_start'], selected_start)
                        selected_start += origin['events_selected']
            summary['diagnostic_rows'] = diagnostic.write()
            destination.cd()
            if root.TNamed('replayFilterDiagnosticsMerge', json.dumps(summary)).Write() <= 0:
                raise OSError('Cannot write diagnostic merge provenance')
            destination.Flush()
            if destination.TestBit(root.TFile.kWriteError):
                raise OSError('ROOT reported an error writing diagnostics')
        with open_root(root, output) as file:
            for name, count in summary['diagnostic_rows'].items():
                if file.Get('diagnostics/' + name).GetEntries() != count:
                    raise ValueError('Written diagnostic tree count differs')
            if json.loads(str(file.Get('replayFilterDiagnosticsMerge').GetTitle())) != summary:
                raise ValueError('Written diagnostic provenance differs')
        for item in plan['inputs']:
            verify(item['files'])
        receipt = dict(version=1, kind=plan['kind'], plan=plan, diagnostics=summary,
                       files={DIAGNOSTICS_NAME: dict(size=output.stat().st_size, sha256=checksum(output))})
        (folder / DIAGNOSTICS_MANIFEST).write_text(json.dumps(receipt, indent=2, allow_nan=False) + '\n')
        if local.exists():
            raise FileExistsError(f'Local output appeared during merging: {local}')
        os.rename(folder, local)
    print(f'Validated local replayfilter diagnostics: {local / DIAGNOSTICS_NAME}', flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--local-output', type=Path, required=True)
    parser.add_argument('--fs-install', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.fs_install / 'newMuonDIS'))
    import ROOT

    ROOT.gROOT.SetBatch(True)
    print('Loading libShipMuDIS.so for replayfilter merger', flush=True)
    loaded = ROOT.gSystem.Load('libShipMuDIS.so')
    print(f'libShipMuDIS.so load result: {loaded}', flush=True)
    if loaded < 0:
        raise RuntimeError('Cannot load libShipMuDIS.so')
    plan = json.loads(args.plan.read_text())
    operation = merge_diagnostics if plan.get('kind') == 'replayfilter_diagnostics' else merge
    operation(ROOT, plan, args.local_output)


if __name__ == '__main__':
    main()
