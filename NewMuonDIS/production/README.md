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
first input and one unsplit job processing 2 input muons for either replay stage,
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
| n_jobs | Maximum input files in sorted order; -1 selects all, 0 selects none |
| n_subjobs | Optional number of ranges per input file; 1 creates an unsplit job |
| events_per_file | Entries to process starting at first_event; Step 1: 250000, others: -1 (all remaining) |
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

Each discovered ROOT file creates its own Ganga job. For filtering and replay,
this means one job per output file of an upstream subjob, not one job per
upstream run directory. `n_jobs` limits those input files; `n_subjobs` divides
the entries of each selected file independently. For example, `n_jobs: 2` and
`n_subjobs: 3` create two independent Ganga jobs with three subjobs each.
The launcher prints each new Ganga job ID and its input path.

All range counts refer to input entries (input muons for NewMuDIS replay).
With a finite `events_per_file` and `n_subjobs`, entries are divided evenly,
including any remainder. With `events_per_subjob`, the last chunk may be smaller;
an explicit `n_subjobs` must agree with the required number of chunks.

For downstream splitting, for example:

```yaml
n_subjobs: 5
events_per_file: 25000
```

This schedules five ranges of 5000 entries. Alternatively, to schedule a fixed
number of fixed-size ranges without claiming the file's total length:

```yaml
n_subjobs: 5
events_per_file: -1
events_per_subjob: 5000
```

This also schedules 25000 entries, **not automatically all entries in the file**.
The planner does not open ROOT data to count entries. Choose ranges to match the
stored entries; filtering may change the entry count. To process all entries
without knowing the count, use `events_per_file: -1`, `n_subjobs: 1`, and omit
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
Existing selected output filenames cause an error before any submission; use a
new production directory or select only missing inputs for a partial rerun.
