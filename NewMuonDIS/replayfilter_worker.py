#!/usr/bin/env python3
"""Run inside FairShip's Pixi environment; publish counts after ROOT and geometry."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-s', type=int, default=0)
    parser.add_argument('-n', type=int, default=-1)
    parser.add_argument('-o', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(Path('replayfilter-config.json').read_text())
    sys.path.insert(0, str(Path(os.environ['FS_INSTALL']) / 'newMuonDIS'))
    from replayChain import filter_chain
    from analyseEvents import Geometry
    import ROOT

    ROOT.gROOT.SetBatch(True)
    print('Loading libShipMuDIS.so for replayfilter worker', flush=True)
    result = ROOT.gSystem.Load('libShipMuDIS.so')
    print(f'libShipMuDIS.so load result: {result}', flush=True)
    if result < 0:
        raise RuntimeError('Cannot load libShipMuDIS.so')
    geometry = args.o.with_name('geo_' + args.o.name.removeprefix('sim_'))
    manifest = args.o.with_suffix('.json')
    if geometry.exists() or manifest.exists():
        raise FileExistsError('Filter geometry/manifest output already exists')
    # A merged event file has one companion geometry. Compare the persisted
    # geometry object, not ROOT file UUIDs/timestamps, before choosing that file.
    signature = None
    for path in dict.fromkeys(config['geometry_files']):
        model = Geometry(ROOT, path)
        model.load(Path(config['sources'][0]))
        current = hashlib.sha256(str(ROOT.TBufferJSON.ConvertToJSON(model.manager)).encode()).hexdigest()
        if signature is not None and signature != current:
            raise ValueError('Replay chain geometries differ; select a consistent replay production')
        signature = current
    counts = filter_chain(ROOT, config['sources'], args.o, config['geometry_files'][0], args.s, args.n)
    shutil.copyfile(config['geometry_files'][0], geometry)
    data = dict(version=2, production=config, filter=counts, output=args.o.name,
                geometry=geometry.name, output_bytes=args.o.stat().st_size,
                geometry_bytes=geometry.stat().st_size,
                source_bytes=sum(Path(path).stat().st_size for path in config['sources']))
    manifest.write_text(json.dumps(data, indent=2) + '\n')
    print(f'Replayfilter manifest: {manifest.name}', flush=True)


if __name__ == '__main__':
    main()
