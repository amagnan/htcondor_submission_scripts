import time
import os
import random
random.seed(os.environ.get("USER"))
startRun = 0 # int(time.time()) + random.randint(0,10000)
evtsPerJob = 50000
evtsToGen = 10000000
nSJ = int(evtsToGen/evtsPerJob)

#beam parameters
smear = 0
paint = 0
xoff = 50
sigma = 8
ecut = 10
prod = 251107

for nS in range(11):
#for nS in range(2):
    myxoff = xoff+nS*sigma
    savestring = f'smear{smear}_paint{paint}_xoffset{myxoff}'
    print(savestring)

    j = Job(name = f'run_fixedTarget_{savestring}-{evtsToGen}events')
    j.application = Executable(exe = File('bashScript.sh'),\
                               args = [savestring, prod, '-o', '"./"', '-n', evtsPerJob,'--no-debug --no-force -e', ecut, '--beam-smear', smear, '--beam-paint', paint, '--x-offset', myxoff])
    j.splitter = ArgSplitter(args = [['-r', startRun + _i] for _i in range(nSJ)], append = True)
    j.outputfiles = [] #LocalFile('*.root')]
    j.backend = Condor()
    # Choose job flavor (runtime “queue”)
    #j.backend.queue = "tomorrow"
    #j.backend.extraopts = ['+JobFlavour="tomorrow"']
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
