#!/usr/bin/env python3
"""Simulate locally, then copy ROOT outputs into a shared destination directory."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def output_tag(source):
    # Inputs are PROD_DATE/JOB_NUMBER/SUBJOB_NUMBER/muonDis_*.root.
    source = Path(source)
    return f'generatedMuDIS_{source.parent.parent.name}_{source.parent.name}'


def main():
    installation, site, source, destination, *options = sys.argv[1:]
    output = Path(destination)
    if not Path(source).is_file():
        raise FileNotFoundError(source)
    tag = output_tag(source)
    expected = [f'{prefix}_{tag}.root' for prefix in ('sim', 'params', 'geo')]
    for name in expected:
        if os.path.lexists(output / name):
            raise FileExistsError(f'Output already exists: {output / name}')
    local_dir = Path.cwd() / 'generator_output'
    local_dir.mkdir()  # Refuse stale outputs from an earlier attempt.
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().with_name('wn_script_pixi.py')),
        '--fs-install', installation, '--site', site,
        '--runfile', 'macro/run_simScript.py', '--',
        '-f', source, '-o', str(local_dir), '--tag', tag, *options,
    ], check=True)
    for name in expected:
        path = local_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f'Simulation completed without creating nonempty {path}')
    output.mkdir(parents=True, exist_ok=True)
    lock = output / f'.{tag}.lock'
    lock.mkdir()
    staged = None
    published = []
    try:
        paths = sorted(local_dir.glob('*.root'))
        for path in paths:
            if os.path.lexists(output / path.name):
                raise FileExistsError(f'Output already exists: {output / path.name}')
        staged = Path(tempfile.mkdtemp(prefix=f'.{tag}.', suffix='.partial', dir=output))
        for path in paths:
            target = staged / path.name
            print(f'Copying {path} to {output / path.name}', flush=True)
            shutil.copyfile(path, target)
            if target.stat().st_size != path.stat().st_size:
                raise RuntimeError(f'Output copy has incorrect size: {target}')
        for path in paths:
            target = output / path.name
            (staged / path.name).rename(target)
            published.append(target)
        print(f'Output copy completed: {output}', flush=True)
    except Exception:
        for path in published:
            path.unlink()
        raise
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
