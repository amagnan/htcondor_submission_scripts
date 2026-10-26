"""Filter a replay TChain while retaining complete events and source metadata."""

import json
import os
from pathlib import Path
import tempfile


def filter_chain(root, sources, output, geometry_file=None, start_event=0, n_events=-1):
    """The caller loads libShipMuDIS once. Entry limits apply to the whole chain."""
    from analyseEvents import Geometry, read_normalization
    from replayFilter import selection_stage
    from MuDISGenerator_postProcessing import _copy_metadata

    sources = [Path(path).resolve() for path in sources]
    output = Path(output).absolute()
    if not sources or len(set(sources)) != len(sources):
        raise ValueError('A replay chain requires a nonempty list of distinct files')
    if start_event < 0 or n_events < -1:
        raise ValueError('Invalid chain entry range')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Output must not exist: {output}')
    normalization, usable = read_normalization(root, sources, 'cbmsim')
    if usable != sources:
        raise ValueError('A replay chain cannot silently skip keyless ROOT files')
    chain = root.TChain('cbmsim')
    records, uuids, schema = [], set(), None
    for path in sources:
        with root.TFile.Open(str(path), 'READ') as source:
            tree = source.Get('cbmsim')
            current = [(branch.GetName(), branch.GetClassName(), branch.GetTitle(),
                        [(leaf.GetName(), leaf.GetTypeName(), leaf.GetLenStatic())
                         for leaf in branch.GetListOfLeaves()]) for branch in tree.GetListOfBranches()]
            if schema is not None and schema != current:
                raise ValueError(f'Replay branch schemas differ: {path}')
            schema = current
            if any(not tree.GetBranch(name) for name in ('MCTrack', 'strawtubesPoint', 'TimeDetPoint')):
                raise ValueError(f'Missing replay selection branches: {path}')
            uuid = str(source.GetUUID().AsString())
            if uuid in uuids:
                raise ValueError(f'Duplicate replay ROOT UUID: {path}')
            uuids.add(uuid)
            records.append(dict(source_file=str(path), source_uuid=uuid, entries_available=int(tree.GetEntries()),
                                source_entry_start=0, source_entry_stop=0, events_processed=0,
                                events_two_tracks=0, events_selected=0))
        if chain.Add(str(path)) != 1:
            raise ValueError(f'Cannot add replay file: {path}')
    total = int(chain.GetEntries())
    if start_event > total:
        raise ValueError(f'start_event {start_event} exceeds the {total} available entries')
    stop = total if n_events == -1 else min(total, start_event + n_events)
    summary = dict(sources=records, entries_available=total, source_entry_start=start_event,
                   source_entry_stop=stop, events_processed=stop - start_event,
                   events_two_tracks=0, events_selected=0)
    geometry = Geometry(root, geometry_file)
    pdg = root.TDatabasePDG.Instance()
    print(f'TChain: {len(sources)} replay files; entries [{start_event}, {stop}) of {total}', flush=True)
    with tempfile.TemporaryDirectory(prefix='.replayChain-', dir=output.parent) as temporary:
        temporary_path = Path(temporary) / 'selected.root'
        # Keep the empty-chain template alive until the output has been written.
        with root.TFile.Open(str(sources[0]), 'READ') as template, \
                root.TFile.Open(str(temporary_path), 'RECREATE') as destination:
            destination.SetCompressionSettings(template.GetCompressionSettings())
            chain.SetBranchStatus('*', True)
            if total and chain.LoadTree(0) < 0:
                raise OSError('Cannot load first replay chain entry')
            destination.cd()
            selected = chain.CloneTree(0) if total else template.Get('cbmsim').CloneTree(0)
            if not selected:
                raise OSError('Cannot clone replay chain schema')
            previous = -1
            for entry in range(start_event, stop):
                local_entry = int(chain.LoadTree(entry))
                if local_entry < 0:
                    raise OSError(f'Cannot load replay chain entry {entry}')
                index = int(chain.GetTreeNumber())
                record = records[index]
                if index != previous:
                    geometry.load(sources[index])
                    record['source_entry_start'] = local_entry
                    previous = index
                if chain.GetEntry(entry) <= 0:
                    raise OSError(f'Cannot read replay chain entry {entry}')
                stage = selection_stage(chain, geometry, pdg)
                record['source_entry_stop'] = local_entry + 1
                record['events_processed'] += 1
                record['events_two_tracks'] += int(stage >= 1)
                summary['events_two_tracks'] += int(stage >= 1)
                if stage == 2:
                    if selected.Fill() < 0:
                        raise OSError(f'Cannot write replay chain entry {entry}')
                    record['events_selected'] += 1
                    summary['events_selected'] += 1
                if (entry - start_event + 1) % 1000 == 0:
                    print(f'Processed {entry - start_event + 1}/{stop - start_event}; selected {summary["events_selected"]}', flush=True)

            # CloneTree only carries the first tree's user information. Import
            # normalization from every input, deduplicating its stable record IDs.
            info = selected.GetUserInfo()
            old = info.FindObject('muDIS_normalization')
            if old:
                info.Remove(old)
            merged = root.TList()
            merged.SetName('muDIS_normalization')
            merged.SetOwner(True)
            root.SetOwnership(merged, False)
            for item in (root.TParameter(int)('version', 1),
                         root.TParameter(int)('preparation_complete', int(normalization['preparation_complete']))):
                root.SetOwnership(item, False)
                merged.Add(item)
            imported = set()
            # Preserve each input's auxiliary keys separately, since equal key
            # names can contain different summaries in different replay files.
            metadata = destination.mkdir('replay_sources')
            for index, path in enumerate(sources):
                with root.TFile.Open(str(path), 'READ') as source:
                    if index == 0:
                        _copy_metadata(source, destination, skip=('cbmsim', 'replayFilter', 'replay_sources'))
                    directory = metadata.mkdir(str(index))
                    _copy_metadata(source, directory, skip=('cbmsim',))
                    user_info = source.Get('cbmsim').GetUserInfo()
                    directory.cd()
                    if user_info.Write('tree_user_info', root.TObject.kSingleKey) <= 0:
                        raise OSError(f'Cannot copy replay tree user information: {path}')
                    original = user_info.FindObject('muDIS_normalization')
                    if original:
                        for item in original:
                            if item.InheritsFrom('TList') and item.GetName() not in imported:
                                imported.add(item.GetName())
                                clone = item.Clone()
                                root.SetOwnership(clone, False)
                                merged.Add(clone)
            info.Add(merged)
            destination.cd()
            if selected.Write('cbmsim', root.TObject.kOverwrite) <= 0:
                raise OSError('Cannot write selected replay chain')
            if root.TNamed('replayFilter', json.dumps(summary)).Write() <= 0:
                raise OSError('Cannot write replay chain provenance')
            destination.Flush()
            if destination.TestBit(root.TFile.kWriteError):
                raise OSError('Error writing selected replay chain')
        with output.open('xb'):
            pass
        try:
            os.replace(temporary_path, output)
        except BaseException:
            output.unlink()
            raise
    print(f'Processed {summary["events_processed"]}; selected {summary["events_selected"]}. Output: {output}', flush=True)
    return summary
