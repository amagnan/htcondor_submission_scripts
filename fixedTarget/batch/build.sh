#!/bin/bash

FS_INSTALL=/afs/cern.ch/user/a/ammagnan/

source /cvmfs/ship.cern.ch/25.09/setUp.sh

export ALIBUILD_WORK_DIR=${FS_INSTALL}/sw
aliBuild build FairShip --always-prefer-system --config-dir $SHIPDIST --defaults release --force-unknown-architecture
#alienv load FairShip/latest > test_config.sh
#alienv load FairShip/latest-AM-targetStudy-release > test_config.sh
alienv load FairShip/latest-AM-targetSensPlanes-release > test_config.sh
