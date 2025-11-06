#!/bin/bash

LOCALDIR=`pwd`
FS_INSTALL=/afs/cern.ch/user/a/ammagnan

#access string to save output files
savestring=$1

# Remove the first argument
shift

export EOS_MGM_URL=root://eospublic.cern.ch
EOSDIR=/eos/experiment/ship/user/ammagnan/TargetProd/251106/$savestring 
mkdir -p $EOSDIR

echo "INFO: eos dir set to "$EOSDIR
source /cvmfs/ship.cern.ch/25.09/setUp.sh

export ALIBUILD_WORK_DIR=${FS_INSTALL}/sw

source ${FS_INSTALL}/htcondor_submission_scripts/fixedTarget/batch/test_config.sh

echo "INFO: Environment set up for FairShip located at " $FAIRSHIP

echo "INFO: Executing: python ${FS_INSTALL}/FairShip/muonShieldOptimization/run_fixedTarget.py" $@

python ${FS_INSTALL}/FairShip/muonShieldOptimization/run_fixedTarget.py $@

echo "INFO: Finished running. These files are on the WN:"
ls -lh *

#cp geofile_full.root $EOSDIR/geofile_full.root
myfile=`ls pythia*.root`
cp $myfile $EOSDIR/.

if (( "$?" != "0" )); then
    echo " --- Problem with copy of ${myfile} file to EOS. Keeping locally."
else
    eossize=`ls -l $EOSDIR/$myfile | awk '{print $5}'`
    localsize=`ls -l ${myfile} | awk '{print $5}'`
    if [ $eossize != $localsize ]; then
        echo " --- Copy of ${myfile} file to eos failed. Localsize = $localsize, eossize = $eossize. Keeping locally..."
    else
        echo " --- Size check done: Localsize = $localsize, eossize = $eossize"
	rm *.root
    fi
fi

echo "INFO: Files copied to eos!"
ls -ltrh $EOSDIR/*
