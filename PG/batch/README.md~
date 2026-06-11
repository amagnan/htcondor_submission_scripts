# Batch submission

1. Change the FS_INSTALL and setup versions to what you want in build.sh, as well as the alienv command. Execute
``` source build.sh```
to create the test_config.sh which contains all the environment corresponding to the FairShip version in your local sw/ and that will be used for running on the batch system.

PS: to run with the stack, you will need to uncomment the alibuild command, and update the environment to use the correct one, and also uncomment relevant ones in bashScript.sh.

2. Make sure the FairShip installation in `bashScript.sh` points to the version you want.

3. Make sure the MassStorageFile location in `gangaScript.py` points to wherever you want the data to go, and the loop on parameters corresponds to your needs.
Also good practice to execute the bashScript.sh interactively first, with all necessary argument as given in gangaScript.py, to test everything is in order before submitting.


4. Make sure you have created a voms proxy for the file registration:
grid-proxy-init

5. Use the gangaSubmit.sh script to submit the job, from a clean shell. This script provides the right (patched) version of ganga for use here.

6. To monitor your jobs use this ganga: /cvmfs/ganga.cern.ch/Ganga/install/ship/bin/ganga . Don't forget to have a valid grid proxy!


