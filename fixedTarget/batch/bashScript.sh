#!/bin/bash

LOCALDIR=`pwd`
FS_INSTALL=/afs/cern.ch/user/a/ammagnan

export EOS_MGM_URL=root://eospublic.cern.ch
EOSDIR=/eos/experiment/ship/user/ammagnan/TargetProd/251106 
mkdir -p $EOSDIR

source /cvmfs/ship.cern.ch/25.09/setUp.sh

export ALIBUILD_WORK_DIR=${FS_INSTALL}/sw

source ${FS_INSTALL}/htcondor_submission_scripts/fixedTarget/batch/test_config.sh

echo "INFO: Environment set up for FairShip located at " $FAIRSHIP

echo "INFO: Executing: python ${FS_INSTALL}/FairShip/muonShieldOptimization/run_fixedTarget.py" $@

python ${FS_INSTALL}/FairShip/muonShieldOptimization/run_fixedTarget.py $@

echo "INFO: Finished running. These files are on the WN:"
ls -lh *

rootdir=`ls -d */ | grep fixedTarget`
cp $rootdir/geofile_full.root $EOSDIR/${rootdir}_geofile_full.root
file=`find $rootdir/. -type f -exec basename {} \; | grep pythia8`
cp $rootdir/$file.root $EOSDIR/${rootdir}_${file}.root
