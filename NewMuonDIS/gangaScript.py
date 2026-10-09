import argparse
import time
import os
import random
from pathlib import Path

FS_INSTALL='/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs/'
SITE = 'CERN'  # or GENT or GRIDPP

pathToFiles='/eos/experiment/ship/simulation/cuda_muons/try_2026_MS5/'
tag='TRY2026'
geofile='/eos/experiment/ship/simulation/cuda_muons/try_2026_MS5/geofile_2609.root'
zmax = 9500 #maximum z position to create DIS events
zmin = 2000 #minimum z position to create DIS events
nDIS = 100
debug=0
prod=261008
outpath='/eos/experiment/ship/user/ammagnan/MuonDIS'
nEvtsPerFile=250000
nEvtsPerJob=25000

parser = argparse.ArgumentParser(description='Prepare Ganga/Condor DIS jobs.')
parser.add_argument('--input-dir', default=pathToFiles,
                    help='Directory containing sim_*.root files, directly or in run subdirectories')
parser.add_argument('--max-files', type=int, default=-1, metavar='N',
                    help='Process the first N input files in sorted order (-1: all, default)')
parser.add_argument('--test', action='store_true',
                    help='Process 1000 events with one job and one subjob')
parser.add_argument('--submit', action='store_true',
                    help='Submit the prepared jobs (default: prepare only)')
args = parser.parse_args()
if args.max_files < -1:
    parser.error('--max-files must be -1 or a non-negative integer')

directory = Path(args.input_dir).expanduser().resolve()
if not directory.is_dir():
    parser.error(f'Not a directory: {directory}')
pattern = "sim_"
exclpattern = "_rec.root"
inputfiles = sorted(
    f for f in directory.rglob("*.root")
    if f.is_file() and pattern in f.name and exclpattern not in f.name
)
if not inputfiles:
    parser.error(f'No matching simulation ROOT files found in {directory}')

count = len(inputfiles)

# Keep the run output folder when it contains one input. Otherwise include
# the file stem so that files in the same directory have separate outputs.
files_per_directory = {}
for inputfile in inputfiles:
    files_per_directory[inputfile.parent] = files_per_directory.get(inputfile.parent, 0) + 1
inputs = []
outputdirs = set()
for inputfile in inputfiles:
    relative = inputfile.relative_to(directory)
    outputdir = relative.parent
    if inputfile.parent == directory or files_per_directory[inputfile.parent] > 1:
        outputdir = outputdir / inputfile.stem
    if outputdir in outputdirs:
        parser.error(f'Inputs map to the same output directory: {outputdir}')
    outputdirs.add(outputdir)
    inputs.append((inputfile, outputdir))

if args.max_files != -1:
    inputs = inputs[:args.max_files]
if args.test:
    nEvtsPerFile = 100
    nEvtsPerJob = 100
    inputs = inputs[:1]
nJ = len(inputs)
nSJ = nEvtsPerFile // nEvtsPerJob

# Set this path to wherever you want the output to go
outdir=f'{outpath}/{tag}/{prod}/'
if args.submit:
    os.makedirs(outdir, exist_ok=True)

config['Output']['MassStorageFile']['uploadOptions']['path'] = f'{outdir}'
config['Output']['MassStorageFile']['uploadOptions']['defaultProtocol'] = f'root://eospublic.cern.ch/{outdir}'

print(config['Output']['MassStorageFile']['uploadOptions']['path'])

print(f"Found {count} files to process in {directory}")
print(f'{nJ} jobs, {nSJ} subjobs per job, {nEvtsPerJob} events per subjob '
      f'({"submit" if args.submit else "prepare only; use --submit to submit"})')

for inputfile, outputdir in inputs:
    print(f'{inputfile} -> {outdir}{outputdir}/{{sjid}}/')

    j = Job(name = f'run sim production number {tag} - input {inputfile.relative_to(directory)} - {nSJ} subruns')
    j.application = Executable(exe = File('wn_script_pixi.py'), args = ['--fs-install',FS_INSTALL,'--runfile', 'newMuonDIS/prepareEvents.py', '--site', SITE, '--', '-n', nEvtsPerJob, '-d', nDIS, '-g', geofile, '-z', zmax, '--z_min', zmin, '--debug', debug, '-f', str(inputfile)])

    #args that depend on subjobs
    print([['-s',f"{_i*nEvtsPerJob}",'-o', f"muonDis_cudaMu_{tag}_evt{_i*nEvtsPerJob}_{(_i+1)*nEvtsPerJob}.root"] for _i in range(nSJ)])
    j.splitter = ArgSplitter(args = [['-s',f"{_i*nEvtsPerJob}",'-o', f"muonDis_cudaMu_{tag}_evt{_i*nEvtsPerJob}_{(_i+1)*nEvtsPerJob}.root"] for _i in range(nSJ)], append = True)

    j.outputfiles = [
        MassStorageFile('*.root',outputfilenameformat=f'{outputdir}/{{sjid}}/{{fname}}')]

    j.backend = Condor()
    j.backend.cdf_options['+MaxRuntime'] = '86000'
    
    # For running at CERN only
    if SITE == 'CERN':
        j.backend.env['EOS_MGM_URL'] = "root://eospublic.cern.ch"
        j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
        
    # Add in the postprocessor to do the file registration
    # cc = CustomChecker(module = 'postprocessor.py')
    #cc = CustomChecker(module = 'postprocessor_master.py', checkSubjobs=False)
    fc = FileChecker(files = ['stdout'], searchStrings = ['Macro finished successfully.'], failIfFound = False, checkMaster=False)
    j.postprocessors.append(fc)
    #j.postprocessors.append(cc)
    j.comment = f'sent {nSJ} subjobs'
    if args.submit:
        j.submit()
