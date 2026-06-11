#!/bin/bash

FS_INSTALL=/afs/cern.ch/user/a/ammagnan/

source /cvmfs/ship.cern.ch/26.03/setUp.sh

#FS_INSTALL=/cvmfs/ship.cern.ch/26.03/
#export WORK_DIR=${FS_INSTALL}/sw/
#source ${FS_INSTALL}/sw/slc9_x86-64/FairShip/latest/etc/profile.d/init.sh

#export ALIBUILD_WORK_DIR=${FS_INSTALL}/sw
source ${FS_INSTALL}/htcondor_submission_scripts/DP/batch/test_config.sh
export ROOT_INCLUDE_PATH=$ROOT_INCLUDE_PATH:$FAIRSHIP/shipdata

echo "ALIBUILD_INSTALL_ROOT set to: " $ALIBUILD_INSTALL_ROOT
echo "ALIBUILD_WORK_DIR set to: " $ALIBUILD_WORK_DIR
echo "PYTHIA8DATA set to: " $PYTHIA8DATA

echo "INFO: Environment set up for FairShip located at " $FAIRSHIP
echo "INFO: Environment set up for FairShip located at " $FS_INSTALL

echo "INFO: Executing: python ${FS_INSTALL}/sw/slc9_x86-64/FairShip/latest/macro/run_simScript.py" $@
python ${FS_INSTALL}/sw/slc9_x86-64/FairShip/latest/macro/run_simScript.py $@


echo "INFO: Finished running. These files are on the WN:"
ls -lh
