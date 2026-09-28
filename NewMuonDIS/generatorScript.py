#!/usr/bin/env python3
"""Prepare one Ganga/Condor NewMuDIS simulation job per filtered ROOT file."""

import argparse
from pathlib import Path
import os
import shlex
import subprocess
import tempfile


# Edit production and detector configuration here, rather than on the command line.
FS_INSTALL = Path('/afs/cern.ch/work/a/ammagnan/FairShip')
SITE = 'CERN'
GANGA = '/cvmfs/ganga.cern.ch/Ganga/install/ship/bin/ganga'
MAX_RUNTIME = 86000
INPUT_PATTERN = '*_filtered_1.root'  # Recursive; use a prepared-file pattern if needed.
OUTPUT_DIR = None  # EOS destination (Path('/eos/...')); None uses INPUT/generated.
# Workers always generate in their local working directory before copying to OUTPUT_DIR.
N_EVENTS = -1  # All stored DIS interactions in each file; --test limits this to 100.
FIRST_EVENT = 0  # Input muon entry, not DIS interaction number.
SEED = 0  # FairShip chooses a time-based seed; nonzero values increment per job.

# --shieldName selects both shield geometry and files/<name>.root in FairShip.
# The current macro supports TRY_2025 and TRY_2026, with no separate shield-map flag.
MUON_SHIELD_NAME = 'TRY_2025'
# Keep this relative to FairShip: ShipFieldMaker prepends VMCWORKDIR.
SPECTROMETER_FIELD_MAP = 'files/2026_05_07_MainSpectrometerField_V21_3000.root'
STRAW_DESIGN = 10
ENABLE_SND = False
DEBUG = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Edit the defaults at the top of generatorScript.py. Preview only unless '
        '--submit is supplied. Input and output paths must be accessible on workers.'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT_DIR,
                        help='EOS copy destination; overrides OUTPUT_DIR '
                             '(default: DIRECTORY/generated). Created after simulation.')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--test', action='store_true',
                        help='First eligible file only, at most 100 DIS interactions')
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    output_dir = (args.output_dir or directory / 'generated').expanduser().resolve()
    installation = FS_INSTALL.expanduser().resolve()
    if not directory.is_dir():
        parser.error(f'Not a directory: {directory}')
    if output_dir == directory or output_dir in directory.parents:
        parser.error('Output directory must not equal or contain the input directory')
    if Path(SPECTROMETER_FIELD_MAP).is_absolute():
        parser.error('SPECTROMETER_FIELD_MAP must be relative to FS_INSTALL')
    for path in (installation / 'macro/run_simScript.py', installation / 'pixi.toml',
                 installation / f'files/{MUON_SHIELD_NAME}.root',
                 installation / SPECTROMETER_FIELD_MAP):
        if not path.is_file():
            parser.error(f'Required file does not exist: {path}')
    if MAX_RUNTIME <= 0 or N_EVENTS == 0 or N_EVENTS < -1 or FIRST_EVENT < 0:
        parser.error('Invalid MAX_RUNTIME, N_EVENTS or FIRST_EVENT configuration')
    if not 0 <= SEED <= 900000000:
        parser.error('SEED must be between 0 and 900000000')
    options = [
        '--NewMuDIS', '-n', str(100 if args.test else N_EVENTS),
        '-i', str(FIRST_EVENT), '--shieldName', MUON_SHIELD_NAME,
        '--field_map', SPECTROMETER_FIELD_MAP, '--strawDesign', str(STRAW_DESIGN),
        '--SND' if ENABLE_SND else '--noSND', '--debug', str(DEBUG),
    ]
    worker = Path(__file__).resolve().with_name('generator_worker.py')
    wrapper = worker.with_name('wn_script_pixi.py')
    jobs = []
    seen = set()
    for source in sorted(directory.rglob(INPUT_PATTERN)):
        resolved = source.resolve()
        if (not source.is_file() or resolved in seen or output_dir in resolved.parents
                or '_backup' in source.stem):
            continue
        seen.add(resolved)
        destination = output_dir / source.relative_to(directory).with_suffix('')
        if os.path.lexists(destination):
            print(f'Skipping existing output: {destination}', flush=True)
            continue
        seed = 0 if SEED == 0 else (SEED - 1 + len(jobs)) % 900000000 + 1
        job_options = [*options, '-s', str(seed)]
        jobs.append((str(source), str(destination), job_options))
        print(f'{source} -> {destination}', flush=True)
        if args.test:
            print('Test mode: first eligible file only, at most 100 DIS interactions.', flush=True)
            print(f'Generate locally in $PWD/generator_output, then copy ROOT files to {destination}',
                  flush=True)
            print('Worker command to submit: ' + shlex.join([
                str(worker), str(installation), SITE, str(source), str(destination),
                *job_options]), flush=True)
            print('Simulation command (inside the FairShip Pixi environment): '
                  + shlex.join(['python3', str(installation / 'macro/run_simScript.py'),
                                '-f', str(source), '-o', 'generator_output',
                                '--tag', 'newmudis', *job_options]), flush=True)
            break
    print(f'{len(jobs)} jobs {"to submit" if args.submit else "(preview; use --submit to submit)"}', flush=True)
    if not args.submit or not jobs:
        return
    # Ganga supplies Job, Executable, File, LocalFile and Condor in its namespace.
    script = f'''
for index, (source, destination, options) in enumerate({jobs!r}):
    j = Job(name='generator_' + str(index))
    j.application = Executable(exe=File({str(worker)!r}), args=[
        {str(installation)!r}, {SITE!r}, source, destination] + options)
    j.inputfiles = [LocalFile({str(wrapper)!r})]
    j.backend = Condor()
    j.backend.cdf_options['+MaxRuntime'] = {str(MAX_RUNTIME)!r}
    if {SITE!r} == 'CERN':
        j.backend.env['EOS_MGM_URL'] = 'root://eospublic.cern.ch'
        j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
    j.comment = source + ' -> ' + destination
    j.submit()
'''
    with tempfile.TemporaryDirectory(prefix='generator_jobs_') as temporary:
        submission = Path(temporary) / 'submit.py'
        submission.write_text(script)
        subprocess.run([GANGA, str(submission)], check=True)


if __name__ == '__main__':
    main()
