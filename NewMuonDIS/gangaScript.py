import time
import os
import random
from pathlib import Path

FS_INSTALL='/afs/cern.ch/work/a/ammagnan/FairShip'
SITE = 'CERN'  # or GENT or GRIDPP

pathToFiles='/eos/experiment/ship/simulation/cuda_muons/try_2025/processed_muons/'
tag='TRY2025'
geofile='/eos/experiment/ship/simulation/cuda_muons/try_2025/processed_muons/geo_cuda_test.root'
zmax = 9600 #maximum z position to create DIS events
nDIS = 1000
debug=0
prod=260805
outpath='/eos/experiment/ship/user/ammagnan/MuonDIS'
nEvtsPerFile=500000
nEvtsPerJob=10000

# Set this path to wherever you want the output to go
outdir=f'{outpath}/{tag}/{prod}/'
os.makedirs(outdir, exist_ok=True)

config['Output']['MassStorageFile']['uploadOptions']['path'] = f'{outdir}'
config['Output']['MassStorageFile']['uploadOptions']['defaultProtocol'] = f'root://eospublic.cern.ch/{outdir}'

print(config['Output']['MassStorageFile']['uploadOptions']['path'])

directory = Path(pathToFiles)
pattern = "sim_"
count = sum(
    1 for f in directory.rglob("*")
    if f.is_file() and pattern in f.name
)

print(f"Found {count} files to process in {pathToFiles}")
nJ = count
nSJ = nEvtsPerFile // nEvtsPerJob

for therun in range(nJ):

    j = Job(name = f'run sim production number {tag} - run {therun} - {nSJ} subruns')
    j.application = Executable(exe = File('wn_script_pixi.py'), args = ['--fs-install',FS_INSTALL,'--runfile', 'muonDIS/prepareEvents.py', '--site', SITE, '--', '-n', nEvtsPerJob, '-d', nDIS, '-g', geofile, '-z', zmax, '--debug', debug, '-f', f"{pathToFiles}/{therun}"])

    #args that depend on subjobs
    print([['-s',f"{_i*nEvtsPerJob}",'-o', f"muonDis_cudaMu_{tag}_{therun}_evt{_i*nEvtsPerJob}_{(_i+1)*nEvtsPerJob}.root"] for _i in range(nSJ)])
    j.splitter = ArgSplitter(args = [['-s',f"{_i*nEvtsPerJob}",'-o', f"muonDis_cudaMu_{tag}_{therun}_evt{_i*nEvtsPerJob}_{(_i+1)*nEvtsPerJob}.root"] for _i in range(nSJ)], append = True)

    j.outputfiles = [
        MassStorageFile('*.root',outputfilenameformat=f'{therun}/{{sjid}}/{{fname}}')]

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
    j.submit()

