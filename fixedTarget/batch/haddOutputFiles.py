import time
import os
import random


startRun = 0 # int(time.time()) + random.randint(0,10000)
evtsPerJob = 50000
evtsToGen = 10000000
nSJ = int(evtsToGen/evtsPerJob)

#beam parameters
smear = 0
paint = 0
xoff = 50
sigma = 16
ecut = 10
prod = 251106

eosdir=f'/eos/experiment/ship/user/ammagnan/TargetProd/{prod}'

os.system("source /cvmfs/ship.cern.ch/25.09/setUp.sh")

for nS in range(11):
#for nS in range(2):
    myxoff = xoff+nS*sigma
    savestring = f'smear{smear}_paint{paint}_xoffset{myxoff}'
    print(savestring)

    os.system(f'rm {savestring}_allRuns.root')
    os.system(f'hadd {savestring}_allRuns.root {eosdir}/{savestring}/pythia*.root')    

    
    #for run in range(nSJ):
    #    filename = f'{eosdir}/{savestring}/pythia8_evtgen_Geant4_{run}_{ecut}.0.root'
    #    print(filename)
