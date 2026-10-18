#!/usr/bin/env python3
import argparse
import json
import tarfile
import subprocess
import shlex
from pathlib import Path

def prepare_analysis(source, config):
    """Isolate one source for analyseEvents' directory discovery, preserving provenance."""
    source = Path(source).resolve(strict=True)
    directory = Path('analysis-input')
    directory.mkdir()
    (directory / source.name).symlink_to(source)
    if config['type'].startswith('disreplay') and not config.get('geometry_file'):
        geometry = source.with_name('geo_' + source.name[4:]).resolve(strict=True)
        (directory / geometry.name).symlink_to(geometry)
    config = dict(config, production_path=str(directory.resolve()))
    Path('analysis-config.json').write_text(json.dumps(config, indent=2, allow_nan=False))


def archive_analysis(directory):
    """Transfer every output, including nested plots, as one Ganga file."""
    directory = Path(directory)
    with (directory / 'run.json').open() as stream:
        manifest = json.load(stream)
    if manifest.get('output_version') != 3:
        raise ValueError('Missing or unsupported analysis completion manifest')
    with tarfile.open(str(directory) + '.tar.gz', 'w:gz') as archive:
        archive.add(directory, arcname=directory.name)


def main():
    parser = argparse.ArgumentParser(
        description="Run FairShip simScript simulation with pixi environment setup"
    )

    # Main configurable inputs
    parser.add_argument(
        "--fs-install",
        help="Base FairShip installation path"
    )

    parser.add_argument(
        "--FairShip_tag",
        help="What tag is used. Defaults to the cvmfs version if not set"
    )

    parser.add_argument(
        "--site",
        default="CERN",
        help="Which site are we running - for setting site-specific options"
    )

    parser.add_argument(
        "--runfile",
        help="Which python file to run (taken from the FairShip dir location)",
        default = "newMuonDIS/prepareEvents.py"
    )

    parser.add_argument('--analysis-source', help='Single ROOT source to stage for analyseEvents')
    parser.add_argument('--analysis-config', help='JSON analyseEvents settings')

    # Everything after this is passed through to the FairShip script
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to runfile"
    )

    args = parser.parse_args()

    current_dir = Path.cwd()

    # Derive defaults if not provided
    FS_INSTALL = args.fs_install or current_dir 
    RUN_SCRIPT = f"{FS_INSTALL}/{args.runfile}"

    fs_tag = args.FairShip_tag
    print(f"INFO: Running at the site {args.site}")
    print(f"INFO: Running with tag {fs_tag}")
    print(f"INFO: Environment set up for FairShip located at {FS_INSTALL}")
    print(f"INFO: Running script is {RUN_SCRIPT}")


    # Safely quote passthrough args
    script_args = args.script_args[1:] if args.script_args[:1] == ["--"] else args.script_args
    if bool(args.analysis_source) != bool(args.analysis_config):
        parser.error('--analysis-source and --analysis-config must be supplied together')
    if args.analysis_source:
        prepare_analysis(args.analysis_source, json.loads(args.analysis_config))
    passthrough = " ".join(shlex.quote(a) for a in script_args)

    print(f"INFO: Executing: bash /cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs python3 {RUN_SCRIPT} {passthrough}")
    #print(f"INFO: Executing: python3 {RUN_SCRIPT} {passthrough}")

    # Build bash command
    command = f"""
    export FS_INSTALL="{FS_INSTALL}"
    export QT_QPA_PLATFORM=offscreen
    export PATH=/cvmfs/ship-nightlies.cern.ch/pixi/0.72.0-rattlerfs-layers-d5a7568/bin:$PATH
    bash /cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs "{FS_INSTALL}" python3 "{RUN_SCRIPT}" {passthrough}
    """

    #export PATH="/afs/cern.ch/work/a/ammagnan/.pixi/bin:$PATH"
    #echo "eval \\\"$(pixi shell-hook --manifest-path {FS_INSTALL}/pixi.toml)\\\""
    #eval "$(pixi shell-hook --manifest-path {FS_INSTALL}/pixi.toml)"
    #python3 "{RUN_SCRIPT}" {passthrough}


    print("Command being run: ",command)
    subprocess.run(["bash", "-c", command], check=True)

    if args.analysis_source:
        archive_analysis(script_args[script_args.index('--output-dir') + 1])

    print("INFO: Finished running. These files are on the WN:")

    subprocess.run(["ls", "-lh"])


if __name__ == "__main__":
    main()


#export PIXI_HOME=/afs/cern.ch/work/a/ammagnan/.pixi
#export PIXI_CACHE_DIR=/afs/cern.ch/work/a/ammagnan/.cache             
