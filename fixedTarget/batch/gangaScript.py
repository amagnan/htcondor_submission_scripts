import time
import os
import random
random.seed(os.environ.get("USER"))
startRun = 0 # int(time.time()) + random.randint(0,10000)
evtsPerJob = 1000000
evtsToGen = 500000000
nSJ = int(evtsToGen/evtsPerJob)

#beam parameters
ecut = 20
prod = 260327

for target_config in ["","_allW","_thinW","_thinW_He20","_thinW_He200"]:
#for target_config in ["_thinW"]:
    savestring = f'target{target_config}'
    print(savestring)
    yamlstring = f'/afs/cern.ch/user/a/ammagnan/workdir/test_target/target_config{target_config}.yaml'

    j = Job(name = f'run_fixedTarget_{savestring}-{evtsToGen}events')
    j.application = Executable(exe = File('bashScript.sh'),\
                               args = [savestring, prod, '-o', '"./"', '-n', evtsPerJob,'--AddPostTargetSensPlane -e', ecut, '--TARGET_YAML', yamlstring])
    j.splitter = ArgSplitter(args = [['-r', startRun + _i] for _i in range(nSJ)], append = True)
    j.outputfiles = [] #LocalFile('*.root')]
    j.backend = Condor()
    # Choose job flavor (runtime “queue”)
    #j.backend.queue = "tomorrow"
    #j.backend.extraopts = ['+JobFlavour="testmatch"']
    j.backend.cdf_options['+MaxRuntime'] = '100000'
    j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
    j.comment = f'{evtsPerJob} events in each of {nSJ} subjobs'
    j.submit()

##Flavor (queue)	Max runtime
##espresso	20 minutes
##microcentury	1 hour
##longlunch	2 hours
##workday	8 hours
##tomorrow	1 day
##testmatch	3 days
##nextweek	1 week
