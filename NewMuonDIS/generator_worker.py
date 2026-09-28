#!/usr/bin/env python3
"""Simulate locally, then publish the complete set of ROOT outputs together."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main():
    installation, site, source, destination, *options = sys.argv[1:]
    output = Path(destination)
    if not Path(source).is_file():
        raise FileNotFoundError(source)
    if os.path.lexists(output):
        raise FileExistsError(f'Output already exists: {output}')
    local_dir = Path.cwd() / 'generator_output'
    local_dir.mkdir()  # Refuse stale outputs from an earlier attempt.
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().with_name('wn_script_pixi.py')),
        '--fs-install', installation, '--site', site,
        '--runfile', 'macro/run_simScript.py', '--',
        '-f', source, '-o', str(local_dir), '--tag', 'newmudis', *options,
    ], check=True)
    for prefix in ('sim', 'params', 'geo'):
        path = local_dir / f'{prefix}_newmudis.root'
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f'Simulation completed without creating nonempty {path}')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name(output.name + '.lock')
    lock.mkdir()
    staged = None
    try:
        if os.path.lexists(output):
            raise FileExistsError(f'Output already exists: {output}')
        staged = Path(tempfile.mkdtemp(prefix=output.name + '.', suffix='.partial',
                                      dir=output.parent))
        for path in sorted(local_dir.glob('*.root')):
            target = staged / path.name
            print(f'Copying {path} to {output / path.name}', flush=True)
            shutil.copyfile(path, target)
            if target.stat().st_size != path.stat().st_size:
                raise RuntimeError(f'Output copy has incorrect size: {target}')
        staged.rename(output)
        staged = None
        print(f'Output copy completed: {output}', flush=True)
    finally:
        try:
            if staged is not None:
                shutil.rmtree(staged)
        finally:
            lock.rmdir()
    # Retain local files on failure for diagnosis or a manual copy retry.
    shutil.rmtree(local_dir)


if __name__ == '__main__':
    main()
