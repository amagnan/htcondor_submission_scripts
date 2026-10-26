"""Adapters around the existing stage parsers, validators and statistics printers."""

import json
from pathlib import Path
import re
import tarfile

import checkAnalysis
import checkFilterOutputs
import checkPrepareEventsOutputs as preparation
import checkReplay
import checkReplayFilter
from jobTiming import print_job_timing, read_job_timing
from prodWorkflow.state import fingerprint, timestamp, verify, write_json


def local_path(value):
    return str(Path(preparation.eos_path(value) or value).resolve())


def condor_reason(settings, attempt, unit):
    directory = Path(settings['workspace']) / str(attempt['job_id'])
    child = attempt['units'][unit]
    if child is not None:
        directory /= str(child)
    try:
        text = (directory / 'output/condorLog').read_text(errors='replace')
    except OSError:
        return ''
    reasons = [line.strip() for line in text.splitlines()
               if any(word in line for word in ('Job removed by', 'Reason:', 'Error:', 'Transfer output files failure'))]
    return reasons[-1] if reasons else ''


def inspect(settings, group, unit, attempt, full=False):
    child = attempt['units'][unit['id']]
    directory = Path(settings['workspace']) / str(attempt['job_id'])
    if child is not None:
        directory /= str(child)
    logfile = directory / 'output/stdout'
    text = logfile.read_text(errors='replace')
    exits = re.findall(r'^Exit code:\s*(-?\d+)\s*$', text, re.MULTILINE)
    if not exits or int(exits[-1]) != 0 or 'INFO: Finished running.' not in text:
        raise ValueError('missing or unsuccessful worker completion')
    transfer = directory / 'output/__postprocesslocations__'
    locations = transfer.read_text()
    if re.search(r'^massstorage\s+\S+\s+ERROR\b', locations, re.MULTILINE):
        raise ValueError('Ganga output transfer failed')
    files = [local_path(item.strip()) for item in re.findall(r'^massstorage\s+\S+\s+(.+?)\s*$', locations, re.MULTILINE)]
    expected = attempt['outputs'][unit['id']]
    if len(files) != len(expected) or set(files) != set(expected):
        raise ValueError('Transferred files differ from the recorded task outputs')
    identities = [fingerprint(path) for path in files]
    spec = unit['spec']
    output = next(path for path in files if Path(path).name == spec['outputs'][0])
    step = settings['step']
    artifact = dict(id=unit['id'], sources=[output], files=identities)
    if step == 'analysis':
        result = checkAnalysis.read_analysis_archive(output)
        result['archive'] = str(result['archive'])
        outputs = re.findall(r'^Output: (.+)\s*$', text, re.MULTILINE)
        if len(outputs) != 1 or result['directory'] != Path(outputs[0].strip()).name:
            raise ValueError('Analysis log and archive names differ')
        checkAnalysis.validate_analysis_sources([result])
        manifest = result['manifest']
        if (manifest['entries_processed'] != spec['count'] or manifest['chain_entry_start'] != spec['first']
                or [local_path(p) for p in manifest['files']] != spec['sources']):
            raise ValueError('Analysis provenance/range differs from its task')
        artifact.update(entries=manifest['entries_processed'], analysis=result)
        stats = dict(processed=manifest['entries_processed'], normalization=manifest['normalization'])
    elif step == 'replayfilter':
        _, record = checkReplayFilter.read_checked_output(logfile, check_metadata=False)
        if record['production'] != spec['filter'] or record['filter']['events_processed'] != spec['count']:
            raise ValueError('Replayfilter manifest differs from its task')
        artifact.update(replay_type=group['inputs']['replay_type'],
                        geometries=[str(Path(output).with_name(record['geometry']))])
        stats = dict(processed=record['filter']['events_processed'],
                     two_tracks=record['filter']['events_two_tracks'], selected=record['filter']['events_selected'],
                     output_bytes=record['output_bytes'], source_bytes=record.get('source_bytes', 0))
    elif step == 'disgen':
        status, stats = preparation.parse_stdout(text, output)
        if stats is None:
            raise ValueError(status)
    elif step == 'disfilter':
        status, stats = checkFilterOutputs.parse_stdout(text)
        if stats is None:
            raise ValueError(status)
    else:
        status, stats = checkReplay.parse_stdout(text)
        if stats is None:
            raise ValueError(status)
        if stats['generated'] != spec['count'] or stats['first_event'] != spec['first']:
            raise ValueError('Replay log range differs from its task')
        artifact.update(replay_type=step, master=spec['sources'][0],
                        geometries=[next(path for path in files if Path(path).name.startswith('geo_'))])
    if step != 'analysis':
        tree = 'MuonDIS' if step in ('disgen', 'disfilter') else 'cbmsim'
        if full and step == 'disgen':
            status, metadata = preparation.root_statistics(output, include_metadata=True)
        else:
            status, metadata = preparation.root_statistics(output, metadata_only=True, tree_name=tree)
        if metadata is None:
            raise ValueError(f'{output}: {status}')
        stats.update(metadata)
        artifact['entries'] = metadata['written']
        artifact['normalization'] = metadata['normalization']
        if step == 'disfilter' and metadata['written'] != stats['selected']:
            raise ValueError('Filter ROOT entries differ from the selected count')
        if step == 'replayfilter' and metadata['written'] != stats['selected']:
            raise ValueError('Replayfilter ROOT entries differ from the selected count')
        if step.startswith('disreplay') and metadata['written'] != stats['generated']:
            raise ValueError('Replay ROOT entries differ from generated count')
    return dict(unit=unit['id'], attempt=attempt['id'], job_id=attempt['job_id'], subjob_id=child,
                artifact=artifact, stats=stats, full=full,
                evidence=[fingerprint(logfile), fingerprint(transfer), *identities],
                timing=read_job_timing(logfile, text))


def normalization_results(results):
    converted = []
    for result in results:
        metadata = result.get('normalization')
        if metadata is None:
            continue
        if isinstance(metadata.get('records'), list):
            records = {item['id']: {k: v for k, v in item.items() if k != 'id'} for item in metadata['records']}
            metadata = dict(complete=metadata['preparation_complete'], records=records)
        converted.append(dict(normalization=metadata))
    return converted


def print_summary(step, successful, metadata=False):
    results = [item['stats'] for item in successful]
    if not results:
        return
    print('Statistics include only checker-approved outputs; SD is the population standard deviation.')
    print(f'{"Quantity":<36} {"Sum":>15} {"Mean/job":>15} {"SD/job":>15}')
    keys = {
        'disgen': [('processed', 'Input entries processed'), ('written', 'MuonDIS entries written'),
                   ('mu_plus', 'Muons mu+'), ('mu_minus', 'Muons mu-'),
                   ('skipped_events', 'Skipped malformed/non-muon'), ('skipped_low_p', 'Skipped low-p muons'),
                   ('no_hits', 'Counted no detector hits'), ('weighted_muons', 'Muons written weighted')],
        'disfilter': [('processed', 'Muon entries processed'), ('selected', 'Muon entries selected'), ('skipped', 'Muon entries skipped')],
        'replayfilter': [('processed', 'Replay events processed'), ('two_tracks', 'Events with >=2 tracks'),
                         ('selected', 'Events selected'), ('output_bytes', 'Filtered ROOT bytes')],
        'analysis': [('processed', 'Analysis entries processed')],
    }.get(step, [('generated', 'DIS events generated'), ('real_time', 'Simulation real time (s)'),
                 ('cpu_time', 'Simulation CPU time (s)')])
    for key, label in keys:
        values = [item[key] for item in results if key in item]
        if values:
            preparation.print_stats(label, values, width=36)
    if step == 'disgen':
        preparation.print_path_statistics(results)
        preparation.print_extra_statistics(results)
    if step in ('disgen', 'disfilter'):
        materials = sorted({key for item in results for key in item.get('materials', {})})
        for material in materials:
            for index, suffix in enumerate(('raw', 'weighted')):
                values = [item['materials'][material][index] for item in results if material in item.get('materials', {})]
                preparation.print_stats(f'DIS {material} {suffix}', values, width=36)
    if step.startswith('disreplay'):
        for collection in checkReplay.HIT_COLLECTIONS:
            preparation.print_stats('Restored ' + collection,
                                    [item['restored_hits'][collection] for item in results], width=36)
        real = sum(item['real_time'] for item in results)
        cpu = sum(item['cpu_time'] for item in results)
        print(f'Simulation CPU / real time: {100 * cpu / real:.2f}%' if real else 'Simulation CPU / real time: undefined')
    if step in ('disfilter', 'replayfilter'):
        before, after = (sum(item[key] for item in results) for key in ('processed', 'selected'))
        print(f'Selection efficiency: {after}/{before}' + (f' ({100 * after / before:.6g}%)' if before else ''))
    print_job_timing([item['timing'] for item in successful])
    if metadata:
        preparation.print_normalization_statistics(normalization_results(results))


def check(state, metadata=False, full=False, refresh=False):
    settings = state.settings
    previous_path = state.path.with_name('checked.json')
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    if previous and previous.get('state_id') != state.data['id']:
        previous = None
    cached = {item['unit']: item for item in previous['successful']} if previous else {}
    successful, problems, records = [], [], []
    for group in state.data['groups']:
        group_results = []
        for unit in group['units']:
            attempts = state.attempts(unit['id'])
            if not attempts:
                problems.append(dict(unit=unit['id'], reason='not submitted'))
                continue
            attempt = attempts[-1]
            try:
                # A manually resubmitted old job may still have its previous stdout.
                if any(a.get('refreshed') and a['statuses'].get(unit['id']) not in ('completed', 'failed', 'killed')
                       for a in attempts):
                    raise ValueError('Ganga attempt is active or its status is unknown; waiting for terminal status')
                item = cached.get(unit['id'])
                reuse = not refresh and item and item['attempt'] == attempt['id'] and (not full or item['full'])
                if reuse:
                    try:
                        verify(item['evidence'])
                    except (OSError, ValueError):
                        reuse = False
                if not reuse:
                    item = inspect(settings, group, unit, attempt, full)
                group_results.append(item)
                successful.append(item)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, EOFError) as error:
                reason = str(error)
                condor = condor_reason(settings, attempt, unit['id'])
                if condor:
                    reason += '; Condor: ' + condor
                problems.append(dict(unit=unit['id'], job_id=attempt['job_id'],
                                     subjob_id=attempt['units'][unit['id']], reason=reason))
        if settings['step'].startswith('disreplay'):
            # A master remains blocked until every planned range passes, including repairs.
            if len(group_results) == len(group['units']):
                artifacts = [item['artifact'] for item in group_results]
                records.append(dict(id=group['id'], sources=[p for item in artifacts for p in item['sources']],
                                    files=[p for item in artifacts for p in item['files']],
                                    geometries=[p for item in artifacts for p in item['geometries']],
                                    entries=sum(item['entries'] for item in artifacts),
                                    master=artifacts[0]['master'], replay_type=settings['step']))
        else:
            records.extend(item['artifact'] for item in group_results)
    if settings['step'] == 'analysis' and records:
        checkAnalysis.validate_analysis_sources([item['analysis'] for item in records])
    # Conflicting preparation UUIDs invalidate an export even without --metadata.
    normalized = normalization_results([item['stats'] for item in successful])
    identities = {}
    for result in normalized:
        for identity, record in result['normalization']['records'].items():
            if identity in identities and identities[identity] != record:
                raise ValueError(f'Conflicting preparation metadata: {identity}')
            identities[identity] = record
    report = dict(version=1, state_id=state.data['id'], revision=state.data['revision'],
                  step=settings['step'], test=settings['test'], checked=timestamp(),
                  complete=bool(state.data['groups']) and not problems,
                  records=records, successful=successful, problems=problems)
    write_json(Path(settings['step_dir']) / 'checked.json', report)
    print(f'{settings["name"]}: Ganga jobs ' + ', '.join(str(a['job_id']) for a in state.data['attempts']))
    print(f'Successful ranges: {len(successful)}/{len(successful) + len(problems)}; exported inputs: {len(records)}')
    print('Checks: logs, exact transfers, file identities and complete preparation metadata. ROOT headers only unless --check-root is selected.')
    print('Unchanged successful checks are cached; --refresh reopens their files.')
    print_summary(settings['step'], successful, metadata)
    if problems:
        print('Jobs/ranges requiring attention:')
        for item in problems:
            print(f'  {item.get("job_id", "unsubmitted")}/{item.get("subjob_id")}: {item["reason"]} [{item["unit"]}]')
    print(f'Next-step input list: {Path(settings["step_dir"]) / "checked.json"}')
    return 0 if report['complete'] else 1
