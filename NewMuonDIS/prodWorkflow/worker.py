#!/usr/bin/env python3
"""Run one readable JSON task, with unbuffered output and native Python errors."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

# In Ganga these modules are copied into the input sandbox alongside this worker.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wn_script_pixi import archive_analysis


def run(spec, prepare_only=False):
    for item in spec['files']:
        info = Path(item['path']).stat()
        if info.st_size != item['size'] or info.st_mtime_ns != item['mtime_ns']:
            raise ValueError(f'Checked input changed: {item["path"]}')
    if spec['step'] == 'analysis':
        config = spec['analysis']
        # Filtered outputs keep their real provenance but discovery needs sim_* aliases.
        directory = Path('analysis-input')
        directory.mkdir()
        for index, source in enumerate(spec['sources']):
            source = Path(source)
            folder = directory / f'{index:06d}'
            folder.mkdir()
            name = 'sim_' + source.name.removeprefix('sim_').replace('_filtered.root', '.root')
            (folder / name).symlink_to(source)
            if not config.get('geometry_file'):
                geometry = source.with_name('geo_' + source.name.removeprefix('sim_'))
                (folder / ('geo_' + name.removeprefix('sim_'))).symlink_to(geometry)
        Path('analysis-config.json').write_text(json.dumps(dict(config, production_path=str(directory.resolve())), indent=2))
    if spec['step'] == 'replayfilter':
        Path('replayfilter-config.json').write_text(json.dumps(spec['filter'], indent=2))
        script = Path(__file__).resolve().with_name(spec['runfile'])
    else:
        script = Path(spec['fs_install']) / spec['runfile']
    command = ['bash', '/cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs', spec['fs_install'],
               'python3', '-u', '-X', 'faulthandler', str(script), *spec['args']]
    print('INFO: Executing: ' + shlex.join(command), flush=True)
    if prepare_only:
        return
    env = dict(os.environ, FS_INSTALL=spec['fs_install'], QT_QPA_PLATFORM='offscreen', PYTHONUNBUFFERED='1')
    subprocess.run(command, env=env, check=True)
    if spec['step'] == 'analysis':
        archive_analysis(spec['output'])
    for name in spec['outputs']:
        if not Path(name).is_file() or Path(name).stat().st_size == 0:
            raise ValueError(f'Missing or empty output: {name}')
    print('INFO: Finished running. These files are on the WN:', flush=True)
    subprocess.run(['ls', '-lh'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', type=Path, help='Readable task JSON printed by the workflow')
    parser.add_argument('--prepare-only', action='store_true', help='Stage config and links; print the direct command without running it')
    args = parser.parse_args()
    run(json.loads(args.task.read_text()), args.prepare_only)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('Exit code: 1', flush=True)
        raise
    print('Exit code: 0', flush=True)
