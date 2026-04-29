import time
import os
import random

FS_INSTALL=/afs/cern.ch/user/a/ammagnan/
SITE = 'CERN'  # or GENT or GRIDPP
# Set this path to wherever you want the output to go
config['Output']['MassStorageFile']['uploadOptions']['path'] = '/eos/experiment/ship/simulation/sig/DP2026/'
config['Output']['MassStorageFile']['uploadOptions']['defaultProtocol'] = 'root://eospublic.cern.ch//eos/experiment/ship/simulation/sig/DP2026/'
# Now set up the random seed, how many events per subjobs and how many events total
user = os.environ.get("USER")
random.seed(user + str(time.time()))
if SITE == 'CERN':  # either h or service account
    run_min = 0
    run_max = 300000000 - 1
elif SITE == 'GRIDPP':
    run_min = 300000000
    run_max = 600000000 - 1
else:
    run_min = 600000000
    run_max = 900000000

evtsPerJob = 500000  # 200000
nJ = 1  # 
nSJ = 1  # fixed number of subjobs per job to register all subjobs on rucio at the same time

startRun = random.randint(run_min, run_max - nJ * nSJ)

for dpmode in ['meson','pbrem','qcd']:
    dpmumlist = []
    if (dpmode=='meson'):
        dpmumlist.append(['pi0','eta','omega','eta1','eta11'])
    else:
        dpmumlist.append('pi0') #irrelevant, will not be used
        
    for dpmum in dpmomlist:
        for dpmass in [0.2,0.5,0.8,1.2,2]:
            for dpeps in [1e-3,1e-4,1e-5]:
        

                for J in range(nJ):
                    j = Job(name = f'run sim production number {J} - {nSJ * evtsPerJob} events')
                    j.application = Executable(exe = File('wn_script.py'), args = ['--fs-install',FS_INSTALL,'--runfile', 'run_simScript.py', '--cvmfs_version', '26.03', '--site', SITE, '--', '-o', '"./"', '-n', evtsPerJob, '--DarkPhoton -A', dpmode, '-m', str(dpmass), '-e', str(dpeps), '--MesonMother', str(dpmum) ])
                    
                    # IMPORTANT: Only put the run seed in the splitter arguments
                    j.splitter = ArgSplitter(args = [['-r', startRun + J * nSJ + _i, '--seed', startRun + J * nSJ + _i] for _i in range(nSJ)], append = True)
                    j.outputfiles = [MassStorageFile('sim_*.root')]
                    j.backend = Condor()
                    j.backend.cdf_options['+MaxRuntime'] = '86000'

                    # For running at CERN only
                    if SITE == 'CERN':
                        j.backend.env['EOS_MGM_URL'] = "root://eospublic.cern.ch"
                        j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'

                    # Add in the postprocessor to do the file registration
                    # cc = CustomChecker(module = 'postprocessor.py')
                    cc = CustomChecker(module = 'postprocessor_master.py', checkSubjobs=False)
                    fc = FileChecker(files = ['stdout'], searchStrings = ['Macro finished successfully.'], failIfFound = False, checkMaster=False)
                    j.postprocessors.append(fc)
                    j.postprocessors.append(cc)
                    j.comment = f'{evtsPerJob} events in each of {nSJ} subjobs'
                    j.submit()
