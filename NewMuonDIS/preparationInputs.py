"""Validate preparation provenance before planning downstream production."""

import json
from pathlib import Path
import subprocess
import sys

from checkPrepareEventsOutputs import root_statistics


RESPONSE_PREFIX = 'PREPARATION_METADATA_RESULT: '


def metadata_worker():
    """Read requests in FairShip's Python, leaving Ganga in its own interpreter."""
    try:
        import ROOT
    except ImportError as error:
        print(RESPONSE_PREFIX + json.dumps({'error': f'cannot import PyROOT: {error}'}), flush=True)
        return 1
    print(RESPONSE_PREFIX + json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        status, result = root_statistics(request['filename'], metadata_only=True,
                                         tree_name=request['tree'])
        print(RESPONSE_PREFIX + json.dumps(dict(filename=request['filename'], status=status,
                                                valid=result is not None)), flush=True)
    return 0


class PreparationInputs:
    def __init__(self, settings, summary):
        self.allow_partial = settings.get('allow_partial', True)
        step = settings['step']
        self.tree = None if step == 'disgen' else 'MuonDIS'
        if step == 'replayfilter' or (step == 'analyse'
                                     and settings['analysis']['type'].startswith('disreplay')):
            self.tree = 'cbmsim'
        self.summary = summary
        self.cache = {}
        self.reported = set()
        self.fs_install = settings.get('fs_install')
        self.worker = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.worker is None:
            return
        try:
            self.worker.stdin.close()
        except OSError:
            pass
        try:
            self.worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
        finally:
            self.worker.stdout.close()
            self.worker = None

    def worker_command(self):
        if not self.fs_install or not (Path(self.fs_install) / 'pixi.toml').is_file():
            raise ValueError('PyROOT is unavailable in the launcher; set fs_install to a FairShip Pixi workspace')
        return ['bash', '/cvmfs/ship-nightlies.cern.ch/pixi/pixi-cvmfs', self.fs_install,
                'python3', '-u', str(Path(__file__).resolve()), '--metadata-worker']

    def worker_response(self):
        for line in self.worker.stdout:
            if line.startswith(RESPONSE_PREFIX):
                try:
                    response = json.loads(line[len(RESPONSE_PREFIX):])
                except ValueError as error:
                    raise ValueError('Invalid response from FairShip metadata reader') from error
                if not isinstance(response, dict) or 'error' in response:
                    raise ValueError(f'FairShip metadata reader failed: {response}')
                return response
            print(line, end='', file=sys.stderr, flush=True)
        raise ValueError('FairShip metadata reader stopped unexpectedly; see its diagnostics above')

    def read(self, filename):
        if self.worker is None:
            status, result = root_statistics(filename, metadata_only=True, tree_name=self.tree)
            if not status.startswith('cannot import PyROOT:'):
                return status, result
            print(f'Reading preparation metadata in the FairShip environment: {self.fs_install}', flush=True)
            try:
                self.worker = subprocess.Popen(self.worker_command(), stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, text=True, bufsize=1)
            except OSError as error:
                raise ValueError(f'Cannot start FairShip metadata reader: {error}') from error
            if self.worker_response() != {'ready': True}:
                raise ValueError('FairShip metadata reader did not initialize')
        try:
            self.worker.stdin.write(json.dumps(dict(filename=filename, tree=self.tree)) + '\n')
            self.worker.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ValueError('FairShip metadata reader stopped before checking the input') from error
        response = self.worker_response()
        if (response.get('filename') != filename or type(response.get('valid')) is not bool
                or not isinstance(response.get('status'), str)):
            raise ValueError('Invalid response from FairShip metadata reader')
        return response['status'], {} if response['valid'] else None

    def accept(self, sources):
        """Keep a whole group, or report every excluded input; never shorten it."""
        if self.tree is None:
            return True
        problems = {}
        for source in sources:
            filename = str(source)
            if filename not in self.cache:
                status, result = self.read(filename)
                self.cache[filename] = status if result is None else None
            if self.cache[filename] is not None:
                problems[filename] = self.cache[filename]
        if not problems:
            return True
        first = next(iter(problems))
        if not self.allow_partial:
            raise ValueError(f'Invalid preparation input {first}: {problems[first]} '
                             '(use --allow-partial to skip affected inputs)')
        for source in sources:
            filename = str(source)
            if filename in self.reported:
                continue
            reason = problems.get(filename, f'group also contains invalid input {first}')
            message = f'Skipped input {filename}: {reason}'
            self.summary['input_warnings'].append(message)
            self.summary['skipped_preparation_inputs'] += 1
            self.reported.add(filename)
            print('WARNING: ' + message, flush=True)
        return False


if __name__ == '__main__':
    if sys.argv[1:] != ['--metadata-worker']:
        raise SystemExit('This module is used by gangaProdScript.py')
    sys.exit(metadata_worker())
