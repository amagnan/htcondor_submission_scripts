# Replay metadata copying fix — 2026-10-23

`_copy_metadata` in FairShip's `python/MuDISGenerator_postProcessing.py`
assumed every stored object inherited from `TObject`. A persisted `TString`
therefore raised `AttributeError` at `obj.InheritsFrom`. The helper now uses
the class recorded in the ROOT key and copies non-TObject values with
`WriteObjectAny`. Directory recursion and tree cloning remain supported.
The user's Ganga `inputfiles` path-string changes were preserved.

## Fix diff

```diff
         obj = source.Get(name)
+        object_class = ROOT.TClass.GetClass(source.GetKey(name).GetClassName())
+        if not object_class:
+            raise OSError(f"Cannot resolve output metadata class for {name}")
         destination.cd()
-        if obj.InheritsFrom("TDirectory"):
+        # TString and other dictionary-backed values need not derive from
+        # TObject, so dispatch using the persisted class rather than obj methods.
+        if not object_class.InheritsFrom("TObject"):
+            if destination.WriteObjectAny(obj, object_class, name) <= 0:
+                raise OSError(f"Cannot copy output metadata {name}")
+            continue
+        if object_class.InheritsFrom("TDirectory"):
             _copy_metadata(obj, destination.mkdir(name, obj.GetTitle()))
             continue
-        if obj.InheritsFrom("TTree"):
+        if object_class.InheritsFrom("TTree"):
             obj = obj.CloneTree(-1, "fast")
```

## Checks run

From `/afs/cern.ch/work/a/ammagnan/FairShip-cvmfs`:

```bash
REPLAY_METADATA_MODULE_DIR=/tmp/ammagnan/replay-metadata-fix timeout 180s pixi run --as-is python3 -u /afs/cern.ch/work/a/ammagnan/htcondor_submission_scripts/NewMuonDIS/tests/test_replayMetadata.py -v
```

Passed: one ROOT regression covering latest key cycles, TString (including
empty strings), vectors, nested metadata, TNamed, auxiliary trees and skipped
keys. An initial test-fixture read using a nested TString path triggered a
PyROOT segmentation fault; navigating with `GetDirectory(...).Get(...)`
corrected the fixture, and the rerun passed.

The same command without `REPLAY_METADATA_MODULE_DIR`, run before installing
the patch, reproduced the reported `TString.InheritsFrom` AttributeError.
After installation, `cmp` confirmed the installed helper is identical to the
tested staged file.

```bash
timeout 180s pixi cvmfs python -u /afs/cern.ch/work/a/ammagnan/htcondor_submission_scripts/NewMuonDIS/tests/test_replayPipeline.py -v
```

Attempted twice, sequentially. Both exited 124 after the timeout, with only
the CVMFS package-availability message and no test progress. Full pipeline
validation of this patch remains pending. The integration fixture now includes
top-level and nested TString metadata from both chained input files.

From NewMuonDIS:

```bash
python3 -m unittest discover -s tests -p 'test_replayProduction.py' -v
git diff --check
```

All 12 production tests passed; these now exercise the `inputfiles` staging.
`git diff --check` also passed in FairShip-cvmfs. No build, simulation or batch
submission was run.
