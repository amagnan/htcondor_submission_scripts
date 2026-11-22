"""Plan and publish replayfilter merges without importing ROOT or Ganga."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from prodWorkflow.replayProduction import complete_masters, read_filter_manifest
from prodWorkflow.state import digest, fingerprint, verify, write_json

ROOT_NAME = 'replayfilter_merged.root'
GEO_NAME = 'geo_replayfilter_merged.root'
MANIFEST_NAME = 'replayfilter_merged.json'
BUNDLE = (ROOT_NAME, GEO_NAME, MANIFEST_NAME)
DIAGNOSTICS_NAME = 'replayfilter_diagnostics.root'
DIAGNOSTICS_MANIFEST = 'replayfilter_diagnostics.json'


def plan_merge(results, test=False, defer_diagnostics=False):
    """Use whole files from the checked analysis manifests, including empty inputs."""
    paths, identities = {}, {}
    for result in results:
        manifest = result['manifest']
        if not manifest['analysis_signature']['type'].startswith('disreplay'):
            raise ValueError('Replayfilter merging requires replay analysis inputs')
        for name in manifest['files']:
            path = Path(name).resolve(strict=True)
            paths.setdefault(str(path), path)
        for source in manifest['source_ranges']:
            path = str(Path(source['path']).resolve(strict=True))
            identity = dict(uuid=source['file_uuid'], entries=source['entries'])
            if path not in paths or identities.setdefault(path, identity) != identity:
                raise ValueError(f'Inconsistent analysis input identity: {path}')
    if not paths:
        raise ValueError('No replayfilter files referenced by the checked analysis inputs')
    inputs, manifests, modes, diagnostics = [], [], set(), set()
    offset = 0
    for name, path in sorted(paths.items()):
        manifest_path = path.with_suffix('.json')
        record = read_filter_manifest(manifest_path)
        if type(record['production']['test']) is not bool or record['production']['test'] != test:
            raise ValueError(f'Replayfilter test/production mode differs: {path}')
        if path.name != record['output']:
            raise ValueError(f'Replayfilter manifest names another output: {path}')
        geometry = path.with_name(record['geometry'])
        count = record['filter']['events_selected']
        if name in identities and identities[name]['entries'] != count:
            raise ValueError(f'Replayfilter entry count differs from analysis: {path}')
        modes.add(record['filter'].get('cross_section', 'stored'))
        diagnostics.update('diagnostics' in source for source in record['filter']['sources'])
        inputs.append(dict(path=name, geometry=str(geometry), manifest=str(manifest_path),
                           files=[fingerprint(file) for file in (path, geometry, manifest_path)],
                           expected_uuid=identities.get(name, {}).get('uuid'),
                           entries=count, entry_start=offset, entry_stop=offset + count))
        offset += count
        manifests.append((manifest_path, record))
    complete_masters(manifests)
    if len(modes) != 1:
        raise ValueError('Cannot merge replayfilter files with different cross-section modes')
    if len(diagnostics) != 1:
        raise ValueError('Cannot merge replayfilter inputs with mixed diagnostic availability')
    common = Path(os.path.commonpath([str(path.parent) for path in paths.values()]))
    if common == Path(common.anchor):
        raise ValueError('Replayfilter inputs have no common production directory')
    plan = dict(version=1, test=test, inputs=inputs, entries=offset,
                cross_section=modes.pop(), diagnostics=diagnostics.pop(),
                destination=str(common / 'merged'), scope='whole replayfilter files')
    if defer_diagnostics:
        plan['defer_diagnostics'] = True
    plan['id'] = digest(plan)
    return plan


def plan_diagnostics(plan):
    """Keep the exact input ordering and offsets of the completed baseline merge."""
    if not plan.get('defer_diagnostics') or not plan['diagnostics']:
        raise ValueError('This replayfilter merge has no deferred diagnostics')
    result = dict(plan, kind='replayfilter_diagnostics', baseline_plan_id=plan['id'],
                  destination=plan['destination'] + '_diagnostics')
    result.pop('id')
    result['id'] = digest(result)
    return result


def bundle_spec(plan):
    if plan.get('kind') == 'replayfilter_diagnostics':
        return 'replayfilter_diagnostics', DIAGNOSTICS_MANIFEST, (DIAGNOSTICS_NAME,)
    return 'replayfilter_merge', MANIFEST_NAME, (ROOT_NAME, GEO_NAME)


def checksum(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def bundle_manifest(directory, plan):
    """A completion manifest is written only after ROOT validation and file close."""
    directory = Path(directory)
    kind, manifest, files = bundle_spec(plan)
    record = json.loads((directory / manifest).read_text())
    if (record.get('version') != 1 or record.get('kind') != kind
            or record.get('plan') != plan or set(record.get('files', {})) != set(files)):
        raise ValueError(f'Merged replayfilter manifest differs from its plan: {directory}')
    for name, expected in record['files'].items():
        path = directory / name
        if path.stat().st_size != expected['size'] or checksum(path) != expected['sha256']:
            raise ValueError(f'Merged replayfilter checksum/size mismatch: {path}')
    return record


def publish(local, plan):
    """Verify the complete bundle before a same-filesystem directory rename."""
    local, destination = Path(local), Path(plan['destination'])
    _, manifest, files = bundle_spec(plan)
    receipt = bundle_manifest(local, plan)
    if destination.exists():
        # A prior transfer may have finished immediately before a state-save failure.
        if bundle_manifest(destination, plan) != receipt:
            raise FileExistsError(f'Different replayfilter merge already exists: {destination}')
        return str(destination / files[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.replayfilter-merge-', dir=destination.parent))
    try:
        for name in (*files, manifest):
            shutil.copyfile(local / name, temporary / name)
        if bundle_manifest(temporary, plan) != receipt or checksum(temporary / manifest) != checksum(local / manifest):
            raise OSError('Replayfilter transfer verification failed')
        if destination.exists():
            raise FileExistsError(f'Replayfilter destination appeared during transfer: {destination}')
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return str(destination / files[0])


def execute(state, plan, local):
    """Save each completed phase; a failed transfer can reuse the local merge."""
    local = Path(local)
    kind, manifest, files = bundle_spec(plan)
    for source in plan['inputs']:
        verify(source['files'])
    previous = state.data.get(kind)
    if previous and (previous['plan'] != plan or previous['local'] != str(local)):
        raise ValueError('Replayfilter merge inputs/destination changed; use a new merger name')
    if not previous:
        if local.exists() or Path(plan['destination']).exists():
            raise FileExistsError('Replayfilter merge destination already exists; use a new merger name or inspect the existing output')
        state.data[kind] = dict(plan=plan, local=str(local), phase='building')
        state.save()
    record = state.data[kind]
    receipt = local / manifest
    if not receipt.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        plan_path = local.with_name(local.name + '-plan.json')
        write_json(plan_path, plan)
        command = [sys.executable, '-u', str(Path(__file__).with_name('mergeReplayFilter.py')),
                   str(plan_path), '--local-output', str(local), '--fs-install', state.settings['fs_install']]
        subprocess.run(command, check=True)
    bundle_manifest(local, plan)
    record['phase'] = 'ready'
    state.save()
    record['output'] = publish(local, plan)
    record['phase'] = 'published'
    record['files'] = [fingerprint(Path(plan['destination']) / name) for name in (*files, manifest)]
    state.save()
    print(f'Published replayfilter merge: {record["output"]}', flush=True)
    return record
