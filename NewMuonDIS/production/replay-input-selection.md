# Checked replay inputs for filtering

Replayfilter previously discovered files on EOS, without checking whether the
upstream replay subjobs had succeeded. File presence could therefore admit an
unfinished master or files left by an earlier failed attempt.

`checkReplay.py --export-valid-inputs` now writes a JSON list of complete
masters and their replay files. Every expected subjob must have a successful,
complete worker log, a matching successful output-transfer record and a
nonempty replay file. The exact expected subjob IDs are checked, so a missing
subjob directory cannot silently shorten a chain. ROOT headers must contain
complete preparation metadata; event baskets are not read. Missing metadata
excludes the entire master from the export.

`replay_inputs` is now required for replayfilter. Paths are relative to the
YAML file. The planner rechecks the exported masters and transfer paths on
each invocation, including when restoring a saved filter plan. It does not
discover additional files on EOS. An empty approved list produces no jobs.

## Current selection

For replay jobs 3640–3659, the checker found 889 successful subjobs out of 900,
with 11 missing stdout files. Requiring whole successful masters selected:

```text
3642 3643 3645 3646 3647 3648 3649 3650 3653 3654 3656 3658
```

These are **12 masters × 45 files = 540 inputs**. Masters 3640, 3641, 3644,
3651, 3652, 3655, 3657 and 3659 were excluded entirely, including their
successful subjobs. The paths are in `replay_inputs_261021.json`, already
configured in `replayfilter.yaml`.

## Refresh and preview

From NewMuonDIS, regenerate the list after upstream jobs finish:

```bash
python3 -u checkReplay.py /afs/cern.ch/user/a/ammagnan/gangadir/workspace/ammagnan/LocalXML --first-job 3640 --num-jobs 20 --expected-subjobs 45 --export-valid-inputs production/replay_inputs_261021.json
python3 -u gangaProdScript.py production/replayfilter.yaml
```

Both commands were run. The checker wrote the manifest and returned 1 because
some upstream jobs remain incomplete; this does not invalidate the exported
complete masters. The preview returned 0 and planned 12 unsplit filter jobs
from 540 files. It submitted no jobs. The checker summary's statistics cover
all successful subjobs; the export applies the stricter whole-master rule.

The export is a snapshot of eligible masters. Refreshing it can add newly
completed masters. For an existing saved filter state, use the normal
`--sync-state --extend-plan` workflow to append them. Old filter states made
without the required checked-input configuration need a new state file.

## Relevant configuration diff

```diff
 replay_type: disreplay_gen
+replay_inputs: replay_inputs_261021.json
```

The planner's input discovery changes from
`directory.rglob(settings['input_pattern'])` to
`checked_replay_inputs(settings)`. Saved filter plans now pass through this
validation before being reused. The Ganga `inputfiles` staging fix is retained.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_checkReplay.py' -v
python3 -m unittest discover -s tests -p 'test_replayProduction.py' -v
python3 -m unittest discover -s tests -p 'test_gangaProdScript.py' -q
python3 -m unittest discover -s tests -p 'test_production*.py' -q
git diff --check
```

The suites passed 12, 15, 13 and 37 tests, respectively. Coverage includes
whole-master exclusion, missing subjob directories, failed transfers, empty
files, unlisted inputs, changed transfer lists, empty exports and revalidation
of saved plans. No ROOT run, C++ build, simulation or batch submission was
performed for this change.
