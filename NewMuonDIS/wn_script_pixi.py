#!/usr/bin/env python3
import argparse
import subprocess
import shlex
from pathlib import Path

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
        default = "muonDIS/prepareEvents.py"
    )

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
    passthrough = " ".join(shlex.quote(a) for a in script_args)

    print(f"INFO: Executing: python3 {RUN_SCRIPT} {passthrough}")

    # Build bash command
    command = f"""
    export FS_INSTALL="{FS_INSTALL}"
    export PATH="/afs/cern.ch/work/a/ammagnan/.pixi/bin:$PATH"
    echo "eval \\\"$(pixi shell-hook --manifest-path {FS_INSTALL}/pixi.toml)\\\""
    eval "$(pixi shell-hook --manifest-path {FS_INSTALL}/pixi.toml)"
    which python3
    python3 "{RUN_SCRIPT}" {passthrough}
    """

    print("Command being run: ",command)
    subprocess.run(["bash", "-c", command], check=True)

    print("INFO: Finished running. These files are on the WN:")

    subprocess.run(["ls", "-lh"])


if __name__ == "__main__":
    main()


#export PIXI_HOME=/afs/cern.ch/work/a/ammagnan/.pixi
#export PIXI_CACHE_DIR=/afs/cern.ch/work/a/ammagnan/.cache             
