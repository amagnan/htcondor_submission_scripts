#!/bin/bash

FS_INSTALL=/afs/cern.ch/user/a/ammagnan/

source /cvmfs/ship.cern.ch/26.03/setUp.sh

export ALIBUILD_WORK_DIR=${FS_INSTALL}/sw
#aliBuild build FairShip --always-prefer-system --config-dir $SHIPDIST --defaults release
alienv load FairShip/latest-master-release > test_config.sh
