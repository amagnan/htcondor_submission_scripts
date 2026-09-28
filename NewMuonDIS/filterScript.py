#!/usr/bin/env python3
"""Prepare one Ganga/Condor filtering job per ROOT input file."""

import argparse
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Pass filterEvents.py options after -- (for example -- -g geometry.root). '
        'Outputs are INPUT_filtered_NUMBER.root; filtered outputs and backups are skipped. '
        'By default only preview jobs; use --submit to submit them.'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--fs-install', type=Path,
                        default=Path('/afs/cern.ch/work/a/ammagnan/FairShip'))
    parser.add_argument('--site', default='CERN')
    parser.add_argument('--filter-option', type=int, choices=(0, 1, 2), default=1,
                        help='0: charged hits at Tr1 (default); 1: two charged tracks at tracking '
                             'and timing planes; 2: any daughter entering the Tr1-to-calorimeter '
                             'volume. A --filter-option after -- overrides this value.')
    parser.add_argument('--max-runtime', type=int, default=86000)
    parser.add_argument('--ganga', default='/cvmfs/ganga.cern.ch/Ganga/install/ship/bin/ganga')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--test', action='store_true',
                        help='Only process the first eligible file (sorted by path), with 100 events; overrides -n/--n_events')
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    args = parser.parse_args(argv[:split])
    filter_args = argv[split + 1:]
    # Input/output ownership belongs to this launcher, not the passthrough options.
    for option in filter_args:
        if (option.split('=')[0] in ('--inputfile', '--outputfile')
                or option.startswith(('-f', '-o')) and not option.startswith('--')):
            parser.error('Do not pass input/output options after --')
    if args.test:
        # argparse uses the last value if an event count was also supplied.
        filter_args.extend(['--n_events', '100'])
    auxiliary = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    auxiliary.add_argument('-g', '--geoFile', default='/eos/experiment/ship/simulation/cuda_muons/try_2025/processed_muons/geo_cuda_test.root')
    auxiliary.add_argument('--field-map', '--field_map',default='/afs/cern.ch/work/a/ammagnan/FairShip/files/2026_05_07_MainSpectrometerField_V21_3000.root')
    auxiliary.add_argument('--muon-shield-field-map',default='/afs/cern.ch/work/a/ammagnan/FairShip/files/TRY_2025.root')
    auxiliary.add_argument('--no-detector-acceptance', action='store_true')
    auxiliary.add_argument('--filter-option', type=int, choices=(0, 1, 2), default=args.filter_option)

    # Parse supplied values separately so disabled acceptance does not require
    # default geometry/maps, while explicit auxiliary options still pass through.
    defaults = {dest: auxiliary.get_default(dest)
                for dest in ('geoFile', 'field_map', 'muon_shield_field_map')}
    auxiliary.set_defaults(geoFile=None, field_map=None, muon_shield_field_map=None)
    known, filter_args = auxiliary.parse_known_args(filter_args)
    if known.no_detector_acceptance and known.filter_option != 0:
        parser.error('Filter options 1 and 2 require detector acceptance')
    filter_args.extend(['--pythia-decays'])
    filter_args.extend(['--filter-option', str(known.filter_option)])
    if known.no_detector_acceptance:
        filter_args.append('--no-detector-acceptance')
    excluded = set()
    for option, dest in (('--geoFile', 'geoFile'), ('--field-map', 'field_map'),
                         ('--muon-shield-field-map', 'muon_shield_field_map')):
        value = getattr(known, dest)
        if value is None and not known.no_detector_acceptance:
            value = defaults[dest]
        if value is None:
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            parser.error(f'Auxiliary file does not exist: {path}')
        excluded.add(path)
        filter_args.extend([option, str(path)])
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        parser.error(f'Not a directory: {directory}')
    installation = args.fs_install.expanduser().resolve()
    if not (installation / 'newMuonDIS/filterEvents.py').is_file():
        parser.error(f'Cannot find {installation}/newMuonDIS/filterEvents.py')
    if args.max_runtime <= 0:
        parser.error('--max-runtime must be positive')
    jobs = []
    seen = set()
    for source in sorted(directory.rglob('*.root')):
        if (not source.is_file() or source.resolve() in excluded | seen
                or re.search(r'_filtered(?:_\d+)?$', source.stem)
                or re.search(r'_backup(?:_\d+)?$', source.stem)):
            continue
        seen.add(source.resolve())
        destination = source.with_name(f'{source.stem}_filtered_{known.filter_option}.root')
        jobs.append((str(source), str(destination)))
        print(f'{source} -> {destination}', flush=True)
        if args.test:
            print('Test mode: first eligible file only, at most 100 events.', flush=True)
            worker_command = [
                str(Path(__file__).resolve().with_name('filter_worker.py')),
                str(installation), args.site, str(source), str(destination), *filter_args,
            ]
            filter_command = [
                'python3', str(installation / 'newMuonDIS/filterEvents.py'),
                '-f', str(source), '-o', str(Path('filter_output') / destination.name), *filter_args,
            ]
            print('Worker command to submit: ' + shlex.join(worker_command), flush=True)
            print('Filter command (inside the FairShip Pixi environment): '
                  + shlex.join(filter_command), flush=True)
            print(f'After filtering, copy local filter_output/{destination.name} to {destination}', flush=True)
            break
    print(f'{len(jobs)} jobs {"to submit" if args.submit else "(preview; use --submit to submit)"}', flush=True)
    if not args.submit or not jobs:
        return
    worker = str(Path(__file__).resolve().with_name('filter_worker.py'))
    wrapper = str(Path(__file__).resolve().with_name('wn_script_pixi.py'))
    # Ganga supplies Job, Executable, File and Condor in its script namespace.
    script = f'''
for index, (source, destination) in enumerate({jobs!r}):
    # Ganga uses the job name in worker filenames; avoid path separators.
    j = Job(name='filter_' + str(index))
    j.application = Executable(exe=File({worker!r}), args=[
        {str(installation)!r}, {args.site!r}, source, destination] + {filter_args!r})
    j.inputfiles = [LocalFile({wrapper!r})]
    j.backend = Condor()
    j.backend.cdf_options['+MaxRuntime'] = {str(args.max_runtime)!r}
    if {args.site!r} == 'CERN':
        j.backend.env['EOS_MGM_URL'] = 'root://eospublic.cern.ch'
        j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
    j.comment = source + ' -> ' + destination
    j.submit()
'''
    with tempfile.TemporaryDirectory(prefix='filter_jobs_') as temporary:
        submission = Path(temporary) / 'submit.py'
        submission.write_text(script)
        subprocess.run([args.ganga, str(submission)], check=True)


if __name__ == '__main__':
    main()
