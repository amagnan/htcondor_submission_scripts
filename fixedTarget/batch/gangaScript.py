import time
import os
import random
random.seed(os.environ.get("USER"))
startRun = 0 # int(time.time()) + random.randint(0,10000)
evtsPerJob = 10 #100000
evtsToGen = 20
nSJ = int(evtsToGen/evtsPerJob)

#beam parameters
smear = 0
paint = 0
xoff = int(50)
ecut = 10
savestring = f'smear{smear}_paint{paint}_xoffset{xoff}'

j = Job(name = f'run_fixedTarget_{savestring}-{evtsToGen}events')
j.application = Executable(exe = File('bashScript.sh'),\
                           args = [savestring, '-o', '"./"', '-n', evtsPerJob,'--no-debug --no-force -e', ecut, '--beam-smear', smear, '--beam-paint', paint, '--x-offset', xoff])
j.splitter = ArgSplitter(args = [['-r', startRun + _i] for _i in range(nSJ)], append = True)
j.outputfiles = [] #LocalFile('*.root')]
j.backend = Condor()
j.backend.cdf_options['+MaxRuntime'] = '50000'
j.backend.cdf_options['accounting_group'] = 'group_u_SHIP.u_ship_cg'
j.comment = f'{evtsPerJob} events in each of {nSJ} subjobs'
j.submit()
