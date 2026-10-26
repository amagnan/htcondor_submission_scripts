# Unsplit replay TChain filtering — 2026-10-23

This supersedes the initial per-replay-subjob filter layout. As clarified,
one unsplit filter job corresponds to **one disgen file**, combining every
`disreplay_gen` file produced from that prepared input. There is no ArgSplitter.

```text
P/C/g/muonDis_example_disgen.root
    -> P/C/g/muonDis_example_disreplay_gen_filtered.root
```

Matching geometry and a version-2 JSON manifest are transferred beside the ROOT
output. The manifest identifies the disgen file and every chained replay source.
Analysis selects the new suffix and groups ten filter jobs' outputs; worker
symlinks supply the filenames expected by FairShip without changing the saved
production basename. Test outputs end in `_filtered_test.root` and are excluded
from normal analysis discovery.

The new `replayChain.py` runs inside the FairShip environment and reuses its
existing selection helpers. No FairShip source files were changed. It preserves
every accepted event branch, rejects inconsistent schemas and duplicate source
UUIDs, combines normalization records by stable IDs, and retains each source's
auxiliary ROOT objects. The worker verifies matching geometry before merging.

## Checks run

From NewMuonDIS:

```bash
python3 -m unittest discover -s tests -p 'test_replayProduction.py' -v
python3 -m unittest discover -s tests -p 'test_*Production.py' -q
python3 -m unittest discover -s tests -p 'test_gangaProdScript.py' -q
python3 -m unittest discover -s tests -p 'test_production*.py' -q
python3 -m py_compile replayChain.py replayfilter_worker.py tests/test_replayPipeline.py
git diff --check
```

The first command passed the ten grouping tests present at that point. The final
combined suites passed **40 + 13 + 37 tests**, including two additional grouping
checks. They cover no-subjob Ganga construction, file lists in the sandbox,
exact output names/directories, missing prepared files/geometry, complete-chain
retries, saved plans, test-output exclusion, and analysis staging/checking.

From `/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs`:

```bash
timeout 180s pixi cvmfs python -u /afs/cern.ch/work/a/ammagnan/htcondor_submission_scripts/NewMuonDIS/tests/test_replayPipeline.py -v
```

The synthetic ROOT integration test passed in 91 seconds. Two seven-event input
files produced one four-event filtered TChain output. It verified the complete
stored event contents from both files, one shared preparation normalization
record plus both distinct replay records, source metadata, analysis of the new
filename, archives and merging. It also checked a range crossing the file
boundary, a zero-survivor selection, an entirely empty chain, and repeated-input
rejection. The library was loaded once for the process.

The CVMFS environment was used because the previous session established that
the local Pixi environment lacks a library dependency required for ShipMuDIS.
No C++ compilation, simulation or batch submission was performed. Ganga job
construction was mocked; real Condor transfers await production submission.
Use a fresh production state file: saved plans for the old split layout are
not valid for this replacement.
