# DIS production

Run one stage at a time with `gangaProdScript.py`. It uses the existing
`wn_script_pixi.py`, `Executable`, `ArgSplitter`, `MassStorageFile` and Condor
flow. `gangaScript.py` is unchanged. Python with PyYAML can preview commands;
Ganga with PyYAML is required to create and submit jobs.

From the NewMuonDIS directory:

```bash
python3 gangaProdScript.py production/disgen.yaml --test
./gangaSubmit.sh production/disgen.yaml --test
./gangaSubmit.sh production/disgen.yaml --test --submit
./gangaSubmit.sh production/disgen.yaml --submit
```

`gangaSubmit.sh` uses the existing SHiP Ganga installation on CVMFS. A YAML first
argument selects gangaProdScript.py; other invocations retain gangaScript.py.

Without `--submit`, no jobs run and no output directories are created. Inside
Ganga, Job objects are still prepared as in gangaScript.py. `--test` selects the
first pending input and one unsplit job processing 2 input muons for either replay stage,
or 100 input entries for generation/filtering, starting from `first_event`.
It does not imply submission. Replay processes all DIS interactions associated
with those 2 muon entries, which may be more than 2 interactions.

Use the other YAML files in the same commands when their inputs are ready:

| Step | YAML | Executable | Default input pattern |
| --- | --- | --- | --- |
| 1 | disgen.yaml | newMuonDIS/prepareEvents.py | *sim_*.root |
| 2 | disreplay_gen.yaml | macro/run_simScript.py | *_disgen.root |
| 3 | disfilter.yaml | newMuonDIS/filterEvents.py | *_disgen.root |
| 4 | disreplay_filter.yaml | macro/run_simScript.py | *_disfilter.root |
| 5 | replayfilter.yaml | newMuonDIS/replayFilter.py | sim_*_disreplay_gen.root |
| 6 | analyse.yaml | newMuonDIS/analyseEvents.py | filtered sim_*_disreplay_gen.root |

Steps 2 and 3 independently consume Step 1; Step 4 consumes Step 3. The script
does not schedule dependencies or wait for upstream jobs to finish.

## Configuration

Each YAML contains exactly one `step`. All other settings have defaults:

| Key | Meaning / default |
| --- | --- |
| tag, production | TRY2026, 261009 |
| output_base | /eos/experiment/ship/user/ammagnan/MuonDIS |
| output_dir | Full production root; defaults to output_base/tag/production |
| input_dir | Step 1 simulation directory; later stages default to output_dir |
| input_pattern | Recursive filename glob, as in the table above |
| n_jobs | Maximum new Ganga master jobs after skipping existing outputs; -1 selects all pending work, 0 selects none |
| masters_per_job | Complete replayfilter masters per analysis chain; production/analyse.yaml uses 10; CLI: --masters-per-job |
| master_depth | replayfilter: number of relative directory levels identifying a disgen master, default 1 |
| replay_type, geometry_file | replayfilter: disreplay_gen (or disreplay_filter), optional shared geometry override |
| files_per_job | Analysis only: positive number of input files per job, default 1; CLI override: --files-per-job |
| skip_existing | Skip complete output sets, default true; false restores the stop-on-existing-output behavior |
| resubmit_incomplete | Default false: preserve empty/partial outputs and skip their ranges. True opts into cleanup and resubmission |
| state_file | Optional JSON manifest path for persistent plans, Ganga IDs and attempt history; default null (file-based mode) |
| retry_failed | With state_file, include tracked failed/killed ranges; default false |
| n_subjobs | Optional number of ranges per input file; 1 creates an unsplit job |
| events_per_file | Entry span used to plan splits, or unsplit entry limit; Step 1: 250000, others: -1 (all remaining) |
| events_per_subjob | Optional positive chunk size; Step 1 defaults to 25000 when n_subjobs is absent |
| first_event | First input entry, default 0 |
| fs_install | Step 1: FairShip-cvmfs; others: FairShip, as in the existing launchers |
| executable | Path relative to fs_install, as in the table above |
| site, max_runtime | CERN, 86000 seconds |
| options | Mapping of executable flags to values, merged with the stage defaults |

Keep the same output settings in all four YAML files. Paths are resolved relative
to the working directory; `~` is expanded for input/output/install paths.
Executable options pass through unchanged: use full paths for geometry/maps,
except replay's `--field_map`, which is relative to FairShip.

The sample executable options reproduce existing launcher defaults. In particular,
**filterScript.py uses TRY_2025 geometry/maps**, while generation/replay use
TRY_2026. Update these together for the production being run. Replay uses the
spectrometer map named in generatorScript.py's commented configuration line.

Boolean `true` adds a flag, `false` or `null` removes a default flag, scalar values
add one argument, and lists add multiple arguments. Prefer the same flag spelling
as the defaults when overriding them. For example `--SND: true` also disables
the default `--noSND`. Input, output, naming and event-range flags are reserved;
use the corresponding top-level settings instead.

The production YAML files explicitly use the fixed seed `12345`: `options.--seed`
for generation and both replay stages, and `options.--pythia-decay-seed` for
filtering's Pythia8 daughter decays. Change these values to select another
reproducible sequence; use a positive integer below `900000000`. Omitting the
generation seed, or using `0` for filtering/replay, selects time-based seeding.
The launcher passes the configured seed unchanged to every job and subjob; it
does not derive independent seeds for different input files or event ranges.
Reproducing a job therefore requires the same inputs, software, geometry, options
and event splitting as well as the same seed. Analysis exposes no seed option.
These settings apply when preparing new jobs; existing Ganga jobs keep their
original arguments, and existing outputs are still skipped by default.

## Splitting and output layout

See the [directory-flow diagram](directory-structure.md) for generation with
20 × 12,500-entry subjobs, filtering, and both replay modes, including exact
output filenames and the separate Ganga log layout.

Each new ROOT input normally creates its own Ganga job. Partial repairs use one
unsplit job per missing range, preserving its original destination. For filtering and replay,
this means one job per output file of an upstream subjob, not one job per
upstream run directory. `n_jobs` limits new master jobs; `n_subjobs` divides
the entries of each selected file independently. For example, `n_jobs: 2` and
`n_subjobs: 3` create two independent Ganga jobs with three subjobs each.
The launcher prints each new Ganga job ID and its input path.

All range counts refer to input entries (input muons for NewMuDIS replay).
With a finite `events_per_file` and `n_subjobs`, start entries are calculated by
dividing the configured span evenly, including any remainder. With
`events_per_subjob`, an explicit `n_subjobs` must agree with the required number
of chunks. Whenever there are multiple subjobs, the last uses `-n -1` to read
all remaining entries from its calculated start, even beyond `events_per_file`.
Its output name uses `all` as the range end. Unsplit jobs retain their configured
entry limit, and `--test` retains its small fixed limit.

For downstream splitting, for example:

```yaml
n_subjobs: 5
events_per_file: 25000
```

This schedules four ranges of 5000 entries and a final range starting at 20000
that reads to the end of the file. The same split can be specified without a
file-length estimate:

```yaml
n_subjobs: 5
events_per_file: -1
events_per_subjob: 5000
```

The planner does not open ROOT data to count entries. Both configurations cover
all entries, including files longer than 25000 entries. To process all entries
in one unsplit job, use `events_per_file: -1`, `n_subjobs: 1`, and omit
`events_per_subjob`. Multiple subjobs with unknown counts and no chunk size are
rejected instead of processing the whole file repeatedly.

Step 1 preserves run directories; flat inputs or multiple files in one run get
an extra input-stem directory, as in gangaScript.py. With multiple ranges, the
job uses ArgSplitter and `outputdir/{sjid}/{fname}`. With one range, including
`--test`, it has no splitter: the range arguments go directly on the executable
and the output format is `outputdir/{fname}`. This rule applies to every stage.
Later stages preserve the upstream directory (including its subjob number),
adding a new subjob level only for further splitting. Selecting a nested input
directory inside output_dir preserves its full production path.

For a source `run7/sim_a.root`, an example under the production root is:

```text
run7/0/muonDis_cudaMu_TRY2026_evt0_25000_disgen.root
run7/0/muonDis_cudaMu_TRY2026_evt0_25000_disfilter.root
run7/0/sim_muonDis_cudaMu_TRY2026_evt0_25000_disreplay_gen.root
run7/0/sim_muonDis_cudaMu_TRY2026_evt0_25000_disreplay_filter.root
```

Replay also writes `params_...root` and `geo_...root` with the same stage suffix.
Splitting Step 3 further produces, for example:

```text
run7/0/0/muonDis_cudaMu_TRY2026_evt0_25000_evt0_5000_disfilter.root
run7/0/1/muonDis_cudaMu_TRY2026_evt0_25000_evt5000_10000_disfilter.root
```

The terminal stage suffix is replaced at each step; range identifiers are kept.
Finite downstream ranges, including `--test`, add a range identifier even for one
subjob. Default scans ignore other stages, reconstruction outputs and backups.
The default scans for `disfilter` and `disreplay_gen` also accept current
gangaScript.py outputs named `muonDis_cudaMu_<tag>_evt<start>_<end>.root`, without
renaming them. For other legacy names, explicitly set an appropriate
`input_pattern`, such as `muonDis_*.root`. Collision checks cover all discovered
inputs before applying n_jobs, so the layout does not depend on job limits.
Existing outputs, including empty or partial sets, are skipped before applying
`n_jobs`, so repeated invocations advance through the remaining work. Cleanup
and resubmission require explicit opt-in. See the batch-production behavior below.

## Replay selection before analysis

Run `production/replayfilter.yaml` after the selected replay jobs have finished,
then `production/analyse.yaml`. Both target production 261021. Each filter job
is **unsplit** and corresponds to **one disgen file** (one generation subjob),
combining all of that file's `disreplay_gen` outputs as a `TChain`.

```bash
python3 gangaProdScript.py production/replayfilter.yaml --verbose
./gangaSubmit.sh production/replayfilter.yaml --submit
python3 checkReplayFilter.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 4000 --num-jobs 10
python3 gangaProdScript.py production/analyse.yaml --verbose
```

For example:

```text
P/C/g/muonDis_example_disgen.root
P/C/g/0/sim_muonDis_example_evt0_1000_disreplay_gen.root
P/C/g/1/sim_muonDis_example_evt1000_all_disreplay_gen.root
    -> one unsplit filter job reading a TChain of those two replay files
P/C/g/muonDis_example_disreplay_gen_filtered.root
P/C/g/geo_muonDis_example_disreplay_gen_filtered.root
P/C/g/muonDis_example_disreplay_gen_filtered.json
```

The planner locates the corresponding prepared file in each replay file's
ancestor directories, matching its stem. No new filter output directory or
Ganga subjob directory is added. `output_dir` must contain those prepared files,
so Ganga transfers the outputs back beside them. The ordered input list is
transferred as a JSON sandbox file, avoiding command-line size limits.

Filtering preserves every branch of accepted events. Normalization records
from all chain members are combined by their stable IDs. Each source's auxiliary
ROOT objects and tree user information are retained under `replay_sources/`;
JSON provenance records its UUID, processed range and selection counts.
Branch schemas and companion geometries must agree across the chain. Empty
selected trees retain their schema and normalization. The checker validates
worker completion, all three transfers, manifests, sizes and chain totals,
and reports event efficiency and ROOT size reduction without reading ROOT events.

`--test` processes at most 100 entries of one chain in one unsplit job and uses
`_disreplay_gen_filtered_test.root` as its suffix. These test outputs are excluded
by the normal analysis pattern; full analysis also rejects test manifests if
they are selected explicitly.

The optional `disreplay_filter` branch analogously groups replay files by their
corresponding `*_disfilter.root` input and uses `_disreplay_filter_filtered`.
Finish upstream production before filtering. A state file freezes chain inputs;
extending a saved plan allows new prepared files but rejects changed input
lists for existing jobs. Retrying a partial output set reruns its whole chain.
Use a fresh state file for this layout; previous per-replay-subjob plans do not apply.

## Analysis jobs and merging

`production/analyse.yaml` adds `step: analyse`, running
`newMuonDIS/analyseEvents.py`. Set `analysis.type` to `disgen`, `disfilter`,
`disreplay_gen` or `disreplay_filter`. The other entries under `analysis` are
settings from `analyseEvents.yaml`, including weights, axes, geometry and plot
rendering. Input, output and entry ranges are controlled by the launcher's
existing top-level settings. `options` is not used for this stage.

```bash
python3 gangaProdScript.py production/analyse.yaml --test
./gangaSubmit.sh production/analyse.yaml --submit
```

The supplied YAML now consumes only completed `replayfilter` outputs and uses
`masters_per_job: 10`: up to ten filtered disgen files form one analysis chain. The final group may
contain fewer files. This is a tunable
placeholder, also available as `--masters-per-job 10`. The input directory
defaults to the production root `P`, selecting only `*_disreplay_gen_filtered.root`.
Missing manifests, inconsistent chain counts and changed file sizes stop planning.
`n_jobs` limits analysis jobs; `masters_per_job` counts unsplit filter jobs (one output each).

For generation-only analysis, omit `masters_per_job` and use `files_per_job`
to group individual input files. Do not set both grouping options.

For grouped replay analysis, Ganga transfers a JSON input list from
`production/.inputs/`; keep these files while jobs may still be prepared or
resubmitted. This avoids command-line limits with thousands of selected files.
The worker stages links to the selected files and their replay geometries,
using the `sim_*_disreplay_gen.root` aliases expected by FairShip discovery.
The manifest still records the resolved production filenames, including the new suffix,
using separate directories for files with identical names. Geometry is required beside each replay
input unless `analysis.geometry_file` supplies an absolute override. Inputs with
missing geometry stop grouped replay planning. In individual-file mode they
are skipped without consuming the job budget; the preview
reports their count and `--verbose` lists their paths. If all matching inputs
lack geometry, no jobs are prepared. `run.json`
records the resolved source paths, ROOT UUIDs, processed ranges and analysis
settings. With grouping, `first_event`, `events_per_file`, `events_per_subjob` and
`n_subjobs` apply to the combined chain, not separately to each file.
Entry counts refer to analysis input entries (simulated events for replay),
and the last split reads to the end of the chain. `--test` always reads
100 entries from one file in an unsplit job, ignoring `files_per_job`.
Analysis of `disgen` also discovers
the legacy `muonDis_cudaMu_*_evtSTART_END.root` names.

Leave `analysis.geometry_file` unset or use `null` to select each replay input's
matching `geo_*.root`. An explicit override applies to **every** input and
prevents that automatic selection. Override paths are expanded and resolved
relative to the launcher's working directory, then checked during planning and
again on the worker. A missing override is an error; it does not silently fall
back to a different geometry. After changing this setting, create a fresh
Ganga job: an existing job retains the configuration encoded in its arguments.

Outputs use an independent top-level directory under the production root:

```text
analysis/<analysis_name>/<source-path-hash>/[subjob-id]/analysis_evtSTART_END.tar.gz
```

The 20-character hash identifies the ordered absolute source paths in a group
and remains stable when limiting jobs. Single-file jobs retain their original
hash and remain stable when selecting a nested input directory. For grouped
jobs, changing the input selection (including adding files or fixing missing
geometry) can change group boundaries. Keep the selection fixed when resuming,
and choose a new `analysis_name` when changing selections, weights, grouping or
splitting. Existing nonempty
archives are skipped before applying the job limit. Each archive contains the **complete**
analysis directory: `run.json`, histogram archive, CSV summaries, `events.jsonl` (one record per selected replay event) and
all nested plots. The worker marks completion only after packaging succeeds;
Ganga transfers the archive using its usual `MassStorageFile` handler.

Check the submitted Ganga jobs, including unsplit jobs:

```bash
python3 checkAnalysis.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 4000 --num-jobs 10
```

The checker reads worker logs, Ganga transfer records and archives, verifies
`run.json` and required files, and rejects mixed analysis signatures and
changed/overlapping source ranges. It does not open ROOT input files. Reading
compressed archives still requires I/O proportional to their size; detailed
histogram and table validation happens in the merger.

To also merge and render the combined plots:

```bash
python3 checkAnalysis.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 4000 --num-jobs 10 \
    --merge-output /path/to/analysis/merged \
    --merger /path/to/FairShip/newMuonDIS/mergeAnalysis.py
```

Run this in the FairShip Python environment with NumPy/PyYAML and matplotlib.
`--no-plots` disables merged plot rendering. The merger handles normalization
record deduplication and recomputes target scaling; the checker does not add an
extra weight factor. Inputs are unpacked into a `merged_inputs` directory
beside `merged`, or into `--unpack-dir`. They are retained so the merged
`run.json` references accessible inputs. The output must be new or empty and
the unpack directory can be new or empty. If it is nonempty, a unique `merge_*`
subdirectory is created to keep previous inputs intact.

For version-6 archives, add `--skip-event-records` to merge only histograms,
cutflows and normalization:

```bash
python3 checkAnalysis.py ~/gangadir/workspace/ammagnan/LocalXML \
    --first-job 4000 --num-jobs 10 \
    --merge-output /path/to/analysis/merged --skip-event-records
```

This skips extracting `events.jsonl` and records it in `omitted_files`; original
archives remain available. Use the same flag with `mergeAnalysis.py` when
merging these reduced directories later. The checker accepts an empty
`events.jsonl` when zero events were processed. Version 6 no longer requires
`pairs.csv` or `tagger_tracks.csv`.

Failed/missing jobs prevent merging by default. `--merge-successful` explicitly
allows merging valid outputs only, but the checker still returns a nonzero
status to report the omitted jobs. Incompatible or overlapping valid inputs
always block merging. `checkReplay.py` retains its simulation-log behavior.

## Preparation diagnostics and metadata

`checkPrepareEventsOutputs.py` now includes the weighted diagnostic counters and
path-length totals from current `prepareEvents.py`, while retaining support for
older raw-counter logs. It reports path-length means as ratios of combined
additive totals, not averages of per-job means. `ALL` excludes `REST`, and
per-material means count only crossing muons.

The default check reads local logs only. Add `--metadata` to open each ROOT
file once and read `MuonDIS` entries and `muDIS_normalization` from tree headers,
without reading event baskets. The summary deduplicates records by UUID and
reports input muons, sum of weights and sum of squared weights by stage.
Missing legacy metadata is reported as unavailable, not as zero normalization.
These input weights are measured before selection and differ from weights of
written muons.

`--check-root` retains the deeper branch scans for written muon weights and DIS
counts. Combine it with `--metadata` to report both in a single file open.
Metadata-only checks do not establish that event baskets are readable. No EOS
latency benchmark is assumed; ROOT access remains opt-in. The existing
`rmcommand.sh` generation behavior is retained; the checker never executes it.


## Producing in batches and repairing missing ranges

Keep the production and split settings fixed, set `n_jobs` to the desired batch
size, and repeat the same invocation when the previous batch has finished:

```bash
python3 gangaProdScript.py production/disgen.yaml
./gangaSubmit.sh production/disgen.yaml --submit
```

`skip_existing: true` is the default. For each original input/range, the planner
checks the exact expected output paths. With the default
`resubmit_incomplete: false`, a range with any existing output is left untouched,
including empty or partial output sets. Empty outputs can be normal when the
configured split extends beyond the available input entries (for example, a
12,500-entry span applied to a file containing only 10,000 entries). The summary
counts complete file sets separately from empty/partial sets without assuming
that the latter indicate failures.

Replay checks the expected `sim_`, `params_`, and `geo_` ROOT files; analysis
checks its directory archive. This is a file-presence check, not content
validation. Ranges with no output files at all remain eligible: the planner does
not open ROOT inputs to determine whether those ranges are beyond EOF.

Fully completed inputs do not consume `n_jobs`. For example, with `n_jobs: 40`,
if the first 40 inputs already have their outputs, the next invocation prepares
40 new jobs from later inputs. If fewer jobs remain, it prepares those and
reports the shortfall. With `n_jobs: -1`, all pending work is selected; `0` selects
nothing. `--test` similarly skips existing test-range outputs and selects at most
one pending test job. Previewing creates neither jobs outside Ganga nor outputs.

A partially completed split is repaired without changing its range arguments
or output paths. If original ranges 0 and 2 exist, ranges 1 and 3 become two
unsplit Ganga jobs writing directly into the original `/1/` and `/3/` output
directories. This avoids Ganga renumbering the remaining subjobs. Each repair
counts as one new master job toward `n_jobs`. For example, one repair plus 39
new split jobs fills a batch of 40 master jobs; it need not contain the same
number of executable ranges as the previous batch. Repair logs are in the new
master job's `output/stdout`; its comment records the original range index.

Cleanup and resubmission are opt-in: pass `--resubmit-incomplete` or set
`resubmit_incomplete: true` in YAML. When an expected output set is partial, contains an
empty file or has a broken link, its range becomes eligible for resubmission.
For a selected incomplete replay range, all surviving files in its expected
`sim_`, `params_`, `geo_` set are removed and the full range is regenerated.
Completely absent ranges are submitted normally; complete sets remain skipped.

Deletion happens **only with `--submit`**, immediately before submitting the
corresponding new Ganga job. Previewing, including inside Ganga, reports the
number of files selected for cleanup and changes no outputs. Add `--verbose`
to list every planned deletion. Cleanup applies only to selected
ranges within the `n_jobs` budget, preserves unrelated files and complete
sibling ranges, and unlinks symlinks without deleting their targets. It rechecks
all files in the affected sets before deleting any; if an output appeared or
changed after planning, submission stops and requires a fresh plan. Directories
or special files occupying expected output paths are warned about and skipped,
never recursively removed.

The default preview prints totals, the input/output roots once, and one line per
planned job showing its relative input path, range count, entry span and output
layout. Per-range warnings and executable commands are hidden unless `--verbose`
(or `-v`) is given. Explicit cleanup requests remain visible as a file/range
count even in the concise preview.

```bash
# Concise read-only preview; existing outputs are preserved by default.
python3 gangaProdScript.py production/disreplay_gen.yaml
# Full commands, per-range warnings, and any planned cleanup paths.
python3 gangaProdScript.py production/disreplay_gen.yaml --verbose
# Submit missing work, leaving all existing output sets untouched.
./gangaSubmit.sh production/disreplay_gen.yaml --submit
# Explicitly opt into removing selected incomplete sets and regenerating them.
./gangaSubmit.sh production/disreplay_gen.yaml --resubmit-incomplete --submit
```

`--no-resubmit-incomplete` explicitly disables cleanup, including when YAML
enables it. With neither CLI flag supplied, YAML is used and defaults to false.

`skip_existing: false` or `--no-skip-existing` remains an explicit strict mode:
it rejects any existing output before submission, even if resubmission is enabled.
`--skip-existing` restores normal skipping/resubmission behavior.

Without `state_file`, output discovery does not track queued/running Ganga jobs.
Wait for a batch to finish before rerunning the same selection, or enable the
persistent tracking described below.

## Persistent production tracking

Set a separate `state_file` for each stage/analysis campaign, on AFS or local
storage, and use that same file for every batch. Relative paths are interpreted
from the launcher's working directory, like other top-level paths.

```yaml
state_file: /path/to/production-state/disreplay_gen.json
n_jobs: 40
```

Alternatively pass `--state-file /path/to/production-state/disreplay_gen.json`.
The first `--submit` or `--sync-state` saves the **whole discovered plan**, even
when `n_jobs` selects only a small batch. The manifest records the configuration,
input groups, entry ranges, output destinations, Ganga repository identity,
master/subjob IDs, status snapshots and every submission attempt. It contains
paths and bookkeeping, not event data or credentials.

```bash
# Read-only preview. Outside Ganga this uses the saved status snapshot.
python3 gangaProdScript.py production/disreplay_gen.yaml --status

# Create the manifest or save the current Ganga repository statuses; no submission.
./gangaSubmit.sh production/disreplay_gen.yaml --sync-state

# Submit the next 40 jobs. Repeat while previous batches are still running.
./gangaSubmit.sh production/disreplay_gen.yaml --n-jobs 40 --submit

# Examine current Ganga repository statuses and individual attempts.
./gangaSubmit.sh production/disreplay_gen.yaml --status --verbose

# Include failed/killed ranges as well as never-submitted work.
./gangaSubmit.sh production/disreplay_gen.yaml --retry-failed --n-jobs 20 --submit

# Also allow cleanup/retry of empty or partial output sets.
./gangaSubmit.sh production/disreplay_gen.yaml --retry-failed --resubmit-incomplete --submit
```

The batch budget is applied after checking tracked status and existing outputs.
Retries create new Ganga jobs, preserve the original output destinations and
retain the old attempt in the manifest. A failed subjob can be retried while
its siblings remain running. `--n-jobs` overrides YAML for this invocation;
resource settings (`max_runtime`, `site`) and retry policies may also change
between batches.

| Tracked range | Default action |
| --- | --- |
| Never submitted | Apply the usual existing-output checks, then submit if eligible |
| Submitted, running, completing, new, or unknown | Hold it, even if outputs are absent or empty |
| Failed/killed | Hold until `--retry-failed`; complete output sets are still skipped, and partial/empty sets additionally require `--resubmit-incomplete` |
| Completed with nonempty expected files | Skip |
| Completed with absent, empty or partial outputs | Preserve until `--resubmit-incomplete`, including jobs that legitimately produced no output |
| Missing Ganga job or mismatched job identity | Hold for inspection; never infer failure from missing files |

These are [Ganga repository statuses](https://ganga.readthedocs.io/en/latest/API/ganga.GangaCore.GPIDev.Lib.Job.Job.html),
not a direct HTCondor query. Keep Ganga's
normal monitoring active (for example in an interactive Ganga session) so it
updates backend status and runs postprocessing. Script-mode Ganga may have
monitoring disabled. `--sync-state` copies the statuses currently known to Ganga;
it does not wait for or trigger a backend monitoring cycle. A stale active
status holds work rather than risking a duplicate submission. Plain-Python
`--status` is explicitly labelled as a saved snapshot.

File completeness still means nonempty expected files, not ROOT/archive content
validation. Continue using the stage-specific check scripts. The manifest
compares YAML settings, not the contents of executable, geometry or input files;
use a new output namespace and state file when changing physics settings or
replacing those files.

### Adding inputs and preserving groups

Normal reruns use the saved plan without rediscovering or regrouping inputs.
This keeps analysis groups and range destinations stable if upstream files
arrive later, or a missing replay geometry is repaired. Append newly eligible
inputs explicitly:

```bash
./gangaSubmit.sh production/analyse.yaml --sync-state --extend-plan
./gangaSubmit.sh production/analyse.yaml --n-jobs 20 --submit
```

Existing groups, including a short final group, stay unchanged; new files form
new groups. Saved inputs that disappear remain in the plan, and submission
stops if a selected input is unavailable. Changing selections, splitting,
`files_per_job`, executable options or analysis settings is rejected for an
existing manifest. Use a separate state/output namespace for that change.
Use a separate state file for `--test` runs as well.

### Registering existing jobs

For an ongoing production, register the previously submitted Ganga master jobs
before launching another tracked batch:

```bash
./gangaSubmit.sh production/disreplay_gen.yaml --sync-state \
    --adopt-job 1200 --adopt-job 1201 --adopt-job 1202
```

Adoption compares full worker arguments, splitting and output layout against
the saved plan, and rejects mismatches. It supports original split jobs and
individual range-repair jobs. It adds an identity marker to the job comment and
records the attempt; it does not submit or resubmit the job. Use the original
production YAML/output root: older jobs used a global MassStorage upload root,
which cannot be recovered from their relative output layout alone. Existing
nonempty outputs need no adoption, but **all outstanding jobs** for this
production must be registered to protect work with no output yet.

### Interrupted submissions and recovery

The launcher takes an exclusive directory lock beside the manifest throughout
planning, cleanup and submission, and writes JSON by atomic replacement. Two
launchers using the same state file cannot claim the same work. The reservation
and Ganga job ID are saved **before** deleting incomplete outputs or calling
`submit()`. If submission raises an exception after backend acceptance, the
attempt remains held until its Ganga status can be resolved.

A missing job or a job left in `new` can be released explicitly after checking
that it has no live backend work:

```bash
./gangaSubmit.sh production/disreplay_gen.yaml --sync-state --release-job 1200
./gangaSubmit.sh production/disreplay_gen.yaml --retry-failed --submit
```

Release is refused for running/submitted jobs and unknown/mismatched identities;
it does not kill or remove Ganga jobs. For failed/killed jobs use
`--retry-failed` directly. If an old attempt is manually resubmitted later,
its active status blocks new attempts too.

If a launcher is killed without releasing its lock, inspect
`STATE.json.lock/owner.json` and remove that lock directory only after the
recorded launcher has stopped. Do not delete or edit the manifest to force a
retry. Tracking protection requires every launcher for a campaign to share
the same state file; independent manifests or legacy file-only submissions
cannot coordinate with it.
