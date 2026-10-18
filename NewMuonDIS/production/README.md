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
| skip_existing | Skip complete output sets, default true; false restores the stop-on-existing-output behavior |
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
Complete existing output sets are skipped before applying `n_jobs`, so repeated
invocations advance through the remaining work. Incomplete output sets cause an
error before submission. See the batch-production behavior below.

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

Each selected event file creates one job. The worker stages links to just that
file and its replay geometry, so the analysis cannot accidentally process other
files in the same upstream directory. Geometry is required beside each replay
input unless `analysis.geometry_file` supplies an absolute override. `run.json`
records the resolved source paths, ROOT UUIDs, processed ranges and analysis
settings. `events_per_file` and `events_per_subjob` count analysis input entries
(simulated events for replay), and the last split reads to EOF. `--test` reads
100 entries from one file in an unsplit job. Analysis of `disgen` also discovers
the legacy `muonDis_cudaMu_*_evtSTART_END.root` names.

Outputs use an independent top-level directory under the production root:

```text
analysis/<analysis_name>/<source-path-hash>/[subjob-id]/analysis_evtSTART_END.tar.gz
```

The 20-character hash identifies the absolute source path and remains stable
when limiting jobs or selecting a nested input directory. Choose a new
`analysis_name` when changing selections, weights or splitting. Existing nonempty
archives are skipped before applying the job limit. Each archive contains the **complete**
analysis directory: `run.json`, histogram archive, CSV tables, pair details and
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
extra weight factor. Inputs are unpacked into a new `merged_inputs` directory
beside `merged`, or into `--unpack-dir`. They are retained so the merged
`run.json` references accessible inputs. The output must be new or empty and
the unpack directory must be new; reruns never silently reuse stale inputs.

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
checks the exact expected output paths. A range is skipped only when every
expected file is a nonempty regular file (or a link to one). Replay requires
all three `sim_`, `params_`, and `geo_` ROOT files; analysis requires its complete
directory archive. This is a file-presence check, not content validation: use
the appropriate check script to identify corrupt or failed outputs.

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

An incomplete replay set, empty file, directory in place of a file, or broken
link stops planning before any submission. Check or move those outputs before
retrying; the launcher does not overwrite them. Set `skip_existing: false` or
pass `--no-skip-existing` to reject all existing outputs, including complete
sets. `--skip-existing` overrides a YAML setting of false.

Output discovery does not track queued/running Ganga jobs. Wait for a batch to
finish before rerunning the same selection, and use Ganga's existing job records
to resubmit failed jobs that are still registered. Use a new production directory
(or `analysis_name` for analysis) when changing settings: output presence alone
does not establish that geometry, cuts or weights match the current YAML. For
concurrent or more automated campaigns, a saved manifest of source, range,
settings, Ganga IDs and validated completion would provide stronger bookkeeping.
