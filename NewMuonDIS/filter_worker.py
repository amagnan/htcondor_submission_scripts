#!/usr/bin/env python3
"""Filter locally, then copy the completed output to its destination."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import uuid


def main():
    installation, site, source, destination, *options = sys.argv[1:]
    output = Path(destination)
    if not Path(source).is_file():
        raise FileNotFoundError(source)
    local_dir = Path.cwd() / 'filter_output'
    local_dir.mkdir(exist_ok=True)
    local_output = local_dir / output.name
    if os.path.lexists(local_output):
        raise FileExistsError(f'Local output already exists: {local_output}')
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().with_name('wn_script_pixi.py')),
        '--fs-install', installation, '--site', site,
        '--runfile', 'newMuonDIS/filterEvents.py', '--',
        '-f', source, '-o', str(local_output), *options,
    ], check=True)
    if not local_output.is_file():
        raise RuntimeError(f'Filter completed without creating {local_output}')
    # Shared-directory lock prevents two filtering jobs from replacing this output.
    lock = output.with_name(output.name + '.lock')
    lock.mkdir()
    staged = output.with_name(output.name + '.' + uuid.uuid4().hex + '.partial')
    backup = None
    try:
        print(f'Copying local output {local_output} to {output}', flush=True)
        # Keep the existing output intact until the full copy has succeeded.
        shutil.copyfile(local_output, staged)
        if staged.stat().st_size != local_output.stat().st_size:
            raise RuntimeError(f'Output copy has incorrect size: {staged}')
        if os.path.lexists(output):
            backup = output.with_name(output.stem + '_backup.root')
            number = 1
            while os.path.lexists(backup):
                backup = output.with_name(f'{output.stem}_backup_{number}.root')
                number += 1
            output.rename(backup)
            print(f'Backed up {output} to {backup}', flush=True)
        try:
            staged.rename(output)
        except OSError:
            if backup is not None and not os.path.lexists(output):
                backup.rename(output)
            raise
        print(f'Output copy completed: {output}', flush=True)
    finally:
        try:
            if staged.exists():
                staged.unlink()
        finally:
            lock.rmdir()
    # On failure, retain the local file for diagnosis or a manual copy retry.
    local_output.unlink()


if __name__ == '__main__':
    main()
