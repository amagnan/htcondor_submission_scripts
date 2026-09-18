#!/usr/bin/env python3
"""Back up an existing output, then filter one input on the worker node."""

from pathlib import Path
import os
import subprocess
import sys


def main():
    installation, site, source, destination, *options = sys.argv[1:]
    output = Path(destination)
    # Shared-directory lock prevents two filtering jobs from replacing this output.
    lock = output.with_name(output.name + '.lock')
    lock.mkdir()
    try:
        if not Path(source).is_file():
            raise FileNotFoundError(source)
        if os.path.lexists(output):
            backup = output.with_name(output.stem + '_backup.root')
            number = 1
            while os.path.lexists(backup):
                backup = output.with_name(f'{output.stem}_backup_{number}.root')
                number += 1
            output.rename(backup)
            print(f'Backed up {output} to {backup}', flush=True)
        subprocess.run([
            sys.executable, str(Path(__file__).resolve().with_name('wn_script_pixi.py')),
            '--fs-install', installation, '--site', site,
            '--runfile', 'newMuonDIS/filterEvents.py', '--',
            '-f', source, '-o', destination, *options,
        ], check=True)
        if not output.is_file():
            raise RuntimeError(f'Filter completed without creating {output}')
    finally:
        lock.rmdir()


if __name__ == '__main__':
    main()
