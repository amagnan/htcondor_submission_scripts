"""Replay-filter grouping and transfer metadata, without ROOT or Ganga imports."""

import hashlib
import json
from pathlib import Path


def checked_replay_inputs(settings):
    """Recheck the complete masters explicitly approved by checkReplay."""
    from checkReplay import valid_master_inputs

    filename = settings.get('replay_inputs')
    if not filename:
        raise ValueError('replayfilter requires replay_inputs from checkReplay.py --export-valid-inputs')
    with Path(filename).open() as stream:
        record = json.load(stream)
    if (not isinstance(record, dict) or record.get('version') != 1
            or not isinstance(record.get('workspace'), str)
            or not Path(record['workspace']).is_absolute()
            or not isinstance(record.get('masters'), list)):
        raise ValueError(f'Invalid checked replay inputs: {filename}')
    sources, seen_jobs, seen_directories = [], set(), set()
    for master in record['masters']:
        if not isinstance(master, dict):
            raise ValueError(f'Invalid replay master in {filename}')
        job_id = master.get('job_id')
        if type(job_id) is not int or job_id < 0 or job_id in seen_jobs:
            raise ValueError(f'Invalid or duplicate replay job in {filename}')
        seen_jobs.add(job_id)
        try:
            current = valid_master_inputs(Path(record['workspace']) / str(job_id),
                                          master.get('expected_subjobs'), check_metadata=False)
        except (OSError, ValueError) as error:
            raise ValueError(f'Replay master {job_id} no longer passes the checker: {error}') from error
        if current != master.get('sources'):
            raise ValueError(f'Replay inputs changed for master {job_id}; regenerate {filename}')
        directory = Path(current[0]).parent.parent
        if directory in seen_directories:
            raise ValueError(f'Duplicate prepared-input directory in {filename}: {directory}')
        seen_directories.add(directory)
        sources.extend(Path(source) for source in current)
    return sorted(sources)


def plan_filter(settings, test, saved, input_check=None):
    if (settings['first_event'] != 0 or settings['events_per_file'] != -1
            or settings['events_per_subjob'] is not None or settings['n_subjobs'] is not None):
        raise ValueError('replayfilter chains whole files; omit n_subjobs/events_per_subjob and use first_event: 0, events_per_file: -1')
    if settings['options']:
        raise ValueError('Use geometry_file and replay_type for replayfilter, not options')
    directory = settings['input_dir']
    if not directory.is_dir():
        raise ValueError(f'Not a directory: {directory}')
    predecessor = 'disgen' if settings['replay_type'] == 'disreplay_gen' else 'disfilter'
    groups = {}
    for source in checked_replay_inputs(settings):
        if (not source.is_file() or not source.name.startswith('sim_')
                or not source.name.endswith('_' + settings['replay_type'] + '.root')
                or directory not in source.parents or not source.match(settings['input_pattern'])
                or 'replayfilter' in source.relative_to(directory).parts):
            raise ValueError(f'Checked replay input does not match this production: {source}')
        prepared = None
        for parent in source.parents:
            matches = []
            for candidate in parent.glob('*_' + predecessor + '.root'):
                stem = candidate.stem[:-len(predecessor) - 1]
                prefix = 'sim_' + stem + '_'
                if candidate.is_file() and source.name.startswith(prefix):
                    matches.append(candidate)
            if len(matches) > 1:
                raise ValueError(f'Ambiguous {predecessor} file for {source}: {matches}')
            if matches:
                prepared = matches[0]
                break
            if parent == directory:
                break
        if prepared is None:
            raise ValueError(f'Cannot find corresponding {predecessor} file above {source}')
        if settings['output_dir'] not in prepared.parents:
            raise ValueError('replayfilter output_dir must contain the disgen files; outputs are written beside them')
        groups.setdefault(prepared, []).append(source)
    for job in saved:
        if groups.get(Path(job['master'])) != job['sources']:
            raise ValueError(f'Replay inputs changed for saved master {job["master"]}; regenerate the checked inputs and plan')
    jobs = list(saved)
    known = {job['master'] for job in saved}
    for prepared, sources in groups.items():
        identity = str(prepared)
        if identity in known:
            continue
        if input_check is not None and not input_check.accept(sources):
            continue
        paths = [str(path.resolve()) for path in sources]
        digest = hashlib.sha256(json.dumps(paths).encode()).hexdigest()
        geometries = [str(Path(settings['geometry_file']).expanduser().resolve()
                          if settings['geometry_file'] else source.with_name('geo_' + source.name[4:]))
                      for source in sources]
        for geometry in geometries:
            if not Path(geometry).is_file():
                raise ValueError(f'Missing replay geometry: {geometry}')
        stem = prepared.stem[:-len(predecessor) - 1]
        name = stem + '_' + settings['replay_type'] + ('_filtered_test.root' if test else '_filtered.root')
        config = dict(sources=paths, geometry_files=geometries, master=identity,
                      master_digest=digest, test=test)
        config_name = 'replayfilter-inputs-' + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20] + '.json'
        folder = prepared.parent.relative_to(settings['output_dir'])
        jobs.append(dict(source=sources[0], sources=sources, master=identity, filter_config=config,
                         args=['--fs-install', settings['fs_install'], '--runfile', settings['executable'],
                               '--site', settings['site'], '--replayfilter-config-file', config_name, '--'],
                         subjobs=[['-s', '0', '-n', '100' if test else '-1', '-o', name]], split=False,
                         output_format=str(folder / '{fname}')))
    return jobs


def read_filter_manifest(path):
    path = Path(path)
    with path.open() as stream:
        result = json.load(stream)
    if result.get('version') != 2:
        raise ValueError(f'Unsupported replayfilter manifest: {path}; expected chain output version 2')
    config, counts = result['production'], result['filter']
    sources = config['sources']
    if (not sources or len(sources) != len(set(sources)) or not Path(config['master']).is_absolute()
            or any(not Path(source).is_absolute() for source in sources)
            or hashlib.sha256(json.dumps(sources).encode()).hexdigest() != config['master_digest']):
        raise ValueError(f'Invalid filter master identity: {path}')
    if [source['source_file'] for source in counts['sources']] != sources:
        raise ValueError(f'Filter sources differ from planned chain: {path}')
    for record in [counts, *counts['sources']]:
        values = [record[key] for key in ('events_processed', 'events_two_tracks', 'events_selected')]
        if any(type(value) is not int or value < 0 for value in values) or values != sorted(values, reverse=True):
            raise ValueError(f'Invalid filter counts: {path}')
        start, stop, total = [record[key] for key in ('source_entry_start', 'source_entry_stop', 'entries_available')]
        if (any(type(value) is not int for value in (start, stop, total))
                or not 0 <= start <= stop <= total or stop - start != values[0]):
            raise ValueError(f'Invalid filter source range: {path}')
    for key in ('events_processed', 'events_two_tracks', 'events_selected', 'entries_available'):
        if counts[key] != sum(record[key] for record in counts['sources']):
            raise ValueError(f'Inconsistent chain totals: {path}')
    uuids = [record['source_uuid'] for record in counts['sources']]
    if not all(uuids) or len(set(uuids)) != len(uuids):
        raise ValueError(f'Missing or duplicate chain source UUID: {path}')
    for key in ('output', 'geometry'):
        name = result[key]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError(f'Invalid filter output name: {path}')
        file = path.parent / name
        if not file.is_file() or file.stat().st_size != result[key + '_bytes'] or file.stat().st_size == 0:
            raise ValueError(f'Missing or changed filtered output: {file}')
    if path != path.with_name(result['output']).with_suffix('.json'):
        raise ValueError(f'Filter manifest name differs from output: {path}')
    return result


def complete_masters(records):
    groups = {}
    seen = set()
    for path, record in records:
        config = record['production']
        if config['master'] in groups:
            raise ValueError(f'Duplicate replayfilter master {config["master"]}')
        for source in record['filter']['sources']:
            identity = source['source_uuid']
            if identity in seen:
                raise ValueError(f'Overlapping replayfilter chain input: {source["source_file"]}')
            seen.add(identity)
        groups[config['master']] = [(path, record)]
    return groups


def checked_analysis_inputs(filename):
    """Recheck exactly the completed filter jobs in an analysis batch."""
    from checkReplayFilter import read_checked_output

    with Path(filename).open() as stream:
        batch = json.load(stream)
    if (not isinstance(batch, dict) or batch.get('version') != 1
            or not isinstance(batch.get('workspace'), str)
            or not Path(batch['workspace']).is_absolute()
            or not isinstance(batch.get('inputs'), list)):
        raise ValueError(f'Invalid checked analysis inputs: {filename}')
    records, sources = [], []
    for item in batch['inputs']:
        if (not isinstance(item, dict) or type(item.get('job_id')) is not int or item['job_id'] < 0
                or not isinstance(item.get('subjob_id'), str)
                or not (item['subjob_id'] == '.' or item['subjob_id'].isdigit())):
            raise ValueError(f'Invalid filter job in {filename}')
        job = str(item['job_id']) + '/' + item['subjob_id']
        logfile = Path(batch['workspace']) / job / 'output/stdout'
        try:
            manifest, record = read_checked_output(logfile, check_metadata=False)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(f'Filter job {job} no longer passes the checker: {error}') from error
        source = manifest.parent / record['output']
        if str(manifest) != item.get('manifest') or str(source) != item.get('output'):
            raise ValueError(f'Filter outputs changed for job {job}; regenerate {filename}')
        if record['production']['test']:
            raise ValueError(f'Test filter output cannot be used in an analysis batch: {source}')
        records.append((manifest, record))
        sources.append(source)
    complete_masters(records)
    return sorted(sources)


def analysis_groups(settings, sources, test=False):
    from gangaProdScript import integer

    count = integer(settings, 'masters_per_job', 1)
    if not settings['analysis']['type'].startswith('disreplay') or settings['files_per_job'] != 1:
        raise ValueError('masters_per_job requires replay analysis and files_per_job: 1')
    records = []
    for source in sources:
        record = read_filter_manifest(source.with_suffix('.json'))
        if record['production']['test'] and not test:
            raise ValueError('Test filter outputs cannot be used for full analysis')
        records.append((source, record))
    masters = complete_masters(records)
    groups = [[path for path, record in group] for master, group in sorted(masters.items())]
    if test:
        return [groups[0][:1]] if groups else []
    return [[source for master in groups[offset:offset + count] for source in master]
            for offset in range(0, len(groups), count)]
