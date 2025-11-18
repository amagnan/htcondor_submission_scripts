import time
import os
import random


startRun = 0 # int(time.time()) + random.randint(0,10000)
evtsPerJob = 25000
evtsToGen = 10000000
nSJ = int(evtsToGen/evtsPerJob)

#beam parameters
smear = 0
paint = 0
xoff = 50
sigma = 8
ecut = 10
prod = 251117

eosdir=f'/eos/experiment/ship/user/ammagnan/TargetProd/{prod}'

#os.system("source /cvmfs/ship.cern.ch/25.09/setUp.sh")
os.system("source /cvmfs/ship-nightlies.cern.ch/pythia8.315/setUp.sh")

for nS in range(11):
#for nS in range(2):
    myxoff = xoff+nS*sigma
    savestring = f'smear{smear}_paint{paint}_xoffset{myxoff}'
    print(savestring)

    os.system(f'rm {prod}_{savestring}_allRuns.root')
    os.system(f'hadd {prod}_{savestring}_allRuns.root {eosdir}/{savestring}/pythia*.root')    
    os.system(f'ls {eosdir}/{savestring}/pythia*.root | wc -l >> saveNfiles.txt')    

    
    #for run in range(nSJ):
    #    filename = f'{eosdir}/{savestring}/pythia8_evtgen_Geant4_{run}_{ecut}.0.root'
    #    print(filename)
