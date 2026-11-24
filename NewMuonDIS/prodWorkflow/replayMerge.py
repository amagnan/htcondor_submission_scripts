"""Plan and publish replayfilter merges without importing ROOT or Ganga."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

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


def plan_diagnostic_parts(plan, max_input_bytes=4 * 1024 ** 3):
    """Histograms first, then whole-input tree parts with baseline-global offsets.

    Compressed input bytes are a sizing guide, not an output size guarantee.
    """
    if max_input_bytes <= 0:
        raise ValueError('Diagnostic tree part size must be positive')
    baseline = plan_diagnostics(plan)
    groups, group, size = [], [], 0
    for item in baseline['inputs']:
        input_bytes = next(file['size'] for file in item['files'] if file['path'] == item['path'])
        if input_bytes > max_input_bytes:
            raise ValueError(f'Input exceeds diagnostic tree part size: {item["path"]}; increase --tree-part-gib')
        if group and size + input_bytes > max_input_bytes:
            groups.append(group)
            group, size = [], 0
        group.append(item)
        size += input_bytes
    if group:
        groups.append(group)
    parts = []
    for name, component, inputs in [('histograms', 'histograms', baseline['inputs']),
                                     *[(f'trees_{i:04d}', 'trees', items) for i, items in enumerate(groups)]]:
        part = dict(baseline, diagnostic_part=name, diagnostic_component=component,
                    inputs=inputs, destination=baseline['destination'] + '/' + name)
        part.pop('id')
        part['id'] = digest(part)
        parts.append(part)
    return parts


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
    _, manifest, _ = bundle_spec(plan)
    record = json.loads((directory / manifest).read_text())
    validate_manifest(directory, plan, record)
    for name, expected in record['files'].items():
        path = directory / name
        if path.stat().st_size != expected['size'] or checksum(path) != expected['sha256']:
            raise ValueError(f'Merged replayfilter checksum/size mismatch: {path}')
    return record


def validate_manifest(directory, plan, record):
    kind, _, files = bundle_spec(plan)
    if (record.get('version') != 1 or record.get('kind') != kind
            or record.get('plan') != plan or set(record.get('files', {})) != set(files)):
        raise ValueError(f'Merged replayfilter manifest differs from its plan: {directory}')


def destination_available(path):
    """A missing or empty real directory can receive an atomic bundle rename."""
    path = Path(path)
    if path.is_relative_to('/eos/experiment'):
        return not eos_entries(path)
    return not path.is_symlink() and (not path.exists() or path.is_dir() and not any(path.iterdir()))


def eos_command(*arguments):
    """Use the EOS namespace server, bypassing FUSE directory rename caches."""
    return subprocess.run(['xrdfs', 'root://eospublic.cern.ch', *map(str, arguments)],
                          check=True, capture_output=True, text=True, timeout=60).stdout


def eos_entries(directory):
    """Return server-side names, or None for an absent directory only."""
    try:
        listing = eos_command('ls', directory)
    except subprocess.CalledProcessError as error:
        if '[3011]' not in (error.stderr or ''):
            raise
        return None
    return {Path(line).name for line in listing.splitlines() if line.strip()}


class EOSChecksumReadError(OSError):
    """EOS rejected a read because its storage checksum was inconsistent."""


def eos_file_digest(path):
    for delay in (0, 5, 15):
        if delay:
            print(f'EOS reported a checksum read error; retrying in {delay}s: {path}', flush=True)
            time.sleep(delay)
        try:
            return read_eos_file_digest(path)
        except EOSChecksumReadError:
            if delay == 15:
                raise


def read_eos_file_digest(path):
    """Hash the server's bytes without downloading another local ROOT file."""
    command = ['timeout', '600s', 'xrdfs', 'root://eospublic.cern.ch', 'cat', str(path)]
    value, size = hashlib.sha256(), 0
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as process:
            try:
                for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b''):
                    value.update(block)
                    size += len(block)
                status = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if status:
                errors.seek(0)
                message = errors.read().decode(errors='replace').strip()
                error = EOSChecksumReadError if '[3007]' in message and 'wrong file checksum' in message else OSError
                raise error(f'EOS verification failed for {path} (exit {status}): {message}')
    return dict(size=size, sha256=value.hexdigest())


def published_manifest(destination, plan):
    """Verify EOS publications through XRootD, independently of FUSE caches."""
    destination = Path(destination)
    if destination.is_relative_to('/eos/experiment'):
        _, manifest, files = bundle_spec(plan)
        names = eos_entries(destination)
        if names != {*files, manifest}:
            raise OSError(f'EOS publication is incomplete at {destination}: {sorted(names or ())}')
        remote = json.loads(eos_command('cat', destination / manifest))
        validate_manifest(destination, plan, remote)
        for name, expected in remote['files'].items():
            print(f'Verifying published EOS file: {destination / name}', flush=True)
            actual = eos_file_digest(destination / name)
            if any(actual[key] != expected[key] for key in ('size', 'sha256')):
                raise ValueError(f'EOS publication checksum/size mismatch: {destination / name}')
        return remote
    return bundle_manifest(destination, plan)


def verify_publication(destination, plan, receipt):
    if published_manifest(destination, plan) != receipt:
        raise OSError(f'Published replayfilter bundle differs at {destination}')


def rename_bundle(temporary, destination):
    """Commit a verified bundle using server-side namespace operations on EOS."""
    temporary, destination = Path(temporary), Path(destination)
    if destination.is_relative_to('/eos/experiment'):
        # rmdir only removes empty directories. Avoid replacing one through
        # FUSE, which can report success while EOS retains the staging name.
        if eos_entries(destination) is not None:
            eos_command('rmdir', destination)
        eos_command('mv', temporary, destination)
    else:
        os.rename(temporary, destination)


def publish(local, plan):
    """Verify the complete bundle before a same-filesystem directory rename."""
    local, destination = Path(local), Path(plan['destination'])
    _, manifest, files = bundle_spec(plan)
    receipt = bundle_manifest(local, plan)
    if destination.is_relative_to('/eos/experiment'):
        return publish_eos(local, plan, receipt)
    if not destination_available(destination):
        # A prior transfer may have finished immediately before a state-save failure.
        if published_manifest(destination, plan) != receipt:
            raise FileExistsError(f'Different replayfilter merge already exists: {destination}')
        return str(destination / files[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.replayfilter-merge-', dir=destination.parent))
    renamed = False
    try:
        for name in (*files, manifest):
            shutil.copyfile(local / name, temporary / name)
        if bundle_manifest(temporary, plan) != receipt or checksum(temporary / manifest) != checksum(local / manifest):
            raise OSError('Replayfilter transfer verification failed')
        if not destination_available(destination):
            raise FileExistsError(f'Replayfilter destination appeared during transfer: {destination}')
        rename_bundle(temporary, destination)
        renamed = True
        verify_publication(destination, plan, receipt)
    finally:
        if not renamed and temporary.exists():
            shutil.rmtree(temporary)
    return str(destination / files[0])


def publish_eos(local, plan, receipt):
    """Finish and verify native XRootD uploads before publishing their directory."""
    destination = Path(plan['destination'])
    _, manifest, files = bundle_spec(plan)
    if not destination_available(destination):
        if published_manifest(destination, plan) != receipt:
            raise FileExistsError(f'Different replayfilter merge already exists: {destination}')
        return str(destination / files[0])
    eos_command('mkdir', '-p', destination.parent)
    temporary = destination.with_name('.replayfilter-merge-' + uuid.uuid4().hex)
    eos_command('mkdir', temporary)
    renamed = False
    try:
        for name in (*files, manifest):
            print(f'Uploading to EOS with checksum verification: {name}', flush=True)
            command = ['xrdcp', '--nopbar', '--posc', '--cksum', 'adler32',
                       str(local / name), 'root://eospublic.cern.ch/' + str(temporary / name)]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
            except subprocess.CalledProcessError as error:
                raise OSError(f'EOS upload failed for {name}: {error.stderr.strip()}') from error
        verify_publication(temporary, plan, receipt)
        if not destination_available(destination):
            raise FileExistsError(f'Replayfilter destination appeared during transfer: {destination}')
        rename_bundle(temporary, destination)
        renamed = True
        verify_publication(destination, plan, receipt)
    finally:
        if not renamed:
            try:
                names = eos_entries(temporary)
                if names is not None:
                    if names - {*files, manifest}:
                        raise ValueError(f'Unexpected files in EOS staging directory: {temporary}')
                    for name in sorted(names):
                        eos_command('rm', temporary / name)
                    eos_command('rmdir', temporary)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'Could not remove EOS staging directory {temporary}: {error}', flush=True)
    return str(destination / files[0])


def release_local_bundle(local, plan, receipt):
    """Remove only the validated local bundle after verified publication."""
    local = Path(local)
    if not local.exists():
        return
    _, manifest, files = bundle_spec(plan)
    if {path.name for path in local.iterdir()} - {*files, manifest}:
        raise ValueError(f'Unexpected files in local diagnostic bundle: {local}')
    # A previous cleanup may have stopped between unlinks. Validate every
    # remaining file against the verified remote receipt before continuing.
    if (local / manifest).exists() and json.loads((local / manifest).read_text()) != receipt:
        raise ValueError(f'Local diagnostic manifest changed before cleanup: {local}')
    for name in files:
        path = local / name
        expected = receipt['files'][name]
        if path.exists() and (path.stat().st_size != expected['size'] or checksum(path) != expected['sha256']):
            raise ValueError(f'Local diagnostic file changed before cleanup: {path}')
    for name in (*files, manifest):
        (local / name).unlink(missing_ok=True)
    local.rmdir()
    print(f'Released local diagnostic bundle: {local}', flush=True)


def execute(state, plan, local, release_local=False):
    """Save each completed phase; a failed transfer can reuse the local merge."""
    local = Path(local)
    kind, manifest, files = bundle_spec(plan)
    state_key = kind + '_' + plan['diagnostic_part'] if 'diagnostic_part' in plan else kind
    for source in plan['inputs']:
        verify(source['files'])
    previous = state.data.get(state_key)
    if previous and (previous['plan'] != plan or previous['local'] != str(local)):
        raise ValueError('Replayfilter merge inputs/destination changed; use a new merger name')
    if previous and previous['phase'] == 'published' and release_local:
        receipt = published_manifest(plan['destination'], plan)
        release_local_bundle(local, plan, receipt)
        print(f'Reusing published diagnostic part: {plan["destination"]}', flush=True)
        return previous
    if not previous:
        if local.exists() or not destination_available(plan['destination']):
            raise FileExistsError('Replayfilter merge destination already exists; use a new merger name or inspect the existing output')
        state.data[state_key] = dict(plan=plan, local=str(local), phase='building')
        state.save()
    record = state.data[state_key]
    receipt = local / manifest
    if not receipt.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        plan_path = local.with_name(local.name + '-plan.json')
        write_json(plan_path, plan)
        command = [sys.executable, '-u', str(Path(__file__).with_name('mergeReplayFilter.py')),
                   str(plan_path), '--local-output', str(local), '--fs-install', state.settings['fs_install']]
        subprocess.run(command, check=True)
    receipt = bundle_manifest(local, plan)
    record['phase'] = 'ready'
    state.save()
    record['output'] = publish(local, plan)
    record['phase'] = 'published'
    destination = Path(plan['destination'])
    if destination.is_relative_to('/eos/experiment'):
        # Publication already verified server-side. Do not stat the final FUSE
        # paths here: they can remain invisible even after a successful rename.
        manifest_bytes = eos_command('cat', destination / manifest).encode()
        identities = dict(receipt['files'])
        identities[manifest] = dict(size=len(manifest_bytes), sha256=hashlib.sha256(manifest_bytes).hexdigest())
        record['files'] = [dict(path=str(destination / name), **identities[name]) for name in (*files, manifest)]
    else:
        record['files'] = [fingerprint(destination / name) for name in (*files, manifest)]
    state.save()
    if release_local:
        release_local_bundle(local, plan, receipt)
    print(f'Published replayfilter merge: {record["output"]}', flush=True)
    return record
