"""No ROOT/EOS/Condor required: exercise planning, checking and Ganga boundaries."""

import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_checkReplay import SUCCESS_LOG
from test_checkPrepareEventsOutputs import SUCCESS_LOG as PREPARE_LOG
from test_checkFilterOutputs import SUCCESS_LOG as FILTER_LOG
from prodWorkflow import checking, config, planning, state, submission, worker, workflow


NORMALIZATION = dict(complete=True, records={'prepared-id': dict(stage='prepareEvents',
                     input_muons=10, sum_weights=12.0, sum_weights_squared=20.0)})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inputs = self.base / 'inputs'
        self.inputs.mkdir()
        (self.inputs / 'sim_a.root').write_bytes(b'input')
        self.workspace = self.base / 'gangadir/workspace/test/LocalXML'
        self.workspace.mkdir(parents=True)
        self.common = dict(production='unit_test', state_dir=str(self.base / 'state'),
                           output_base=str(self.base / 'outputs'), workspace=str(self.workspace))
        (self.base / 'workflow.yaml').write_text(yaml.safe_dump(self.common))

    def settings(self, step='disgen', test=False, **extra):
        data = dict(step=step)
        if step == 'disgen':
            data.update(input_dir=str(self.inputs), events_per_file=10, events_per_subjob=4)
        data.update(extra)
        path = self.base / f'{step}.yaml'
        path.write_text(yaml.safe_dump(data))
        return config.load(path, test)

    def exported(self, step, records, complete=True, test=False):
        settings = self.settings(step, test=test)
        saved = state.State(settings)
        saved.save()
        report = dict(version=1, state_id=saved.data['id'], revision=saved.data['revision'],
                      step=step, test=test, complete=complete, records=records, successful=[], problems=[])
        state.write_json(saved.path.with_name('checked.json'), report)
        return saved

    def record(self, entries=10, name='prepared.root', identity='source'):
        path = self.base / name
        path.write_bytes(b'checked ROOT')
        return dict(id=identity, sources=[str(path)], files=[state.fingerprint(path)], entries=entries)

    def api(self, fail=False):
        registry = {}
        def new_job(**kwargs):
            job = SimpleNamespace(id=100 + len(registry) * 7, status='new', subjobs=[],
                                  comment='', inputfiles=[], postprocessors=[], **kwargs)
            def do_submit():
                # Reservation must be durable before the backend is called.
                stored = json.loads(self.current_state.path.read_text())
                self.assertEqual(stored['attempts'][-1]['job_id'], job.id)
                self.assertEqual(stored['attempts'][-1]['phase'], 'reserved')
                job.status = 'submitted'
                if fail:
                    raise RuntimeError('backend interrupted')
            job.submit = do_submit
            registry[job.id] = job
            return job
        return SimpleNamespace(Job=new_job, Executable=lambda **kw: SimpleNamespace(**kw),
                               File=lambda x: x, ArgSplitter=lambda **kw: SimpleNamespace(**kw),
                               MassStorageFile=lambda name, **kw: SimpleNamespace(name=name, **kw),
                               Condor=lambda: SimpleNamespace(cdf_options={}, env={}),
                               FileChecker=lambda **kw: SimpleNamespace(**kw), jobs=lambda x: registry[x],
                               registry=registry, config={'Configuration': dict(gangadir=str(self.base / 'gangadir'), user='test', repositorytype='LocalXML'),
                                                         'Output': {'MassStorageFile': {'uploadOptions': {}}}})

    def prepared(self, settings=None):
        saved = state.State(settings or self.settings())
        saved.add_groups(planning.plan(saved.settings))
        self.current_state = saved
        return saved

    def submitted(self, settings):
        saved = self.prepared(settings)
        api = self.api()
        with contextlib.redirect_stdout(io.StringIO()):
            submission.submit(saved, planning.select(saved), api)
        return saved, api

    def replay_logs(self, saved, omit=()):
        for attempt in saved.data['attempts']:
            for group in saved.data['groups']:
                for unit in group['units']:
                    if unit['id'] not in attempt['units'] or unit['id'] in omit:
                        continue
                    spec = unit['spec']
                    paths = attempt['outputs'][unit['id']]
                    for path in paths:
                        output = Path(path)
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(b'valid fake ROOT')
                    child = attempt['units'][unit['id']]
                    directory = self.workspace / str(attempt['job_id'])
                    if child is not None:
                        directory /= str(child)
                    directory /= 'output'
                    directory.mkdir(parents=True, exist_ok=True)
                    log = SUCCESS_LOG.replace('sim_replay.root', spec['output'])
                    import re
                    log = re.sub(r'Generating\s+\d+\s+DIS events, first event\s+\d+',
                                 f'Generating {spec["count"]} DIS events, first event {spec["first"]}', log)
                    (directory / 'stdout').write_text(log)
                    (directory / '__postprocesslocations__').write_text(''.join(f'massstorage *.root {p}\n' for p in paths))

    def metadata(self, filename, **kwargs):
        # Count is encoded in each task's output name.
        import re
        first, stop = map(int, re.search(r'_evt(\d+)_(\d+)_', str(filename)).groups())
        return 'successful', dict(written=stop - first, normalization=deepcopy(NORMALIZATION))

    def check(self, saved, **kwargs):
        with patch.object(checking.preparation, 'root_statistics', side_effect=self.metadata), contextlib.redirect_stdout(io.StringIO()):
            return checking.check(saved, **kwargs)

    def test_all_shipped_configs_load_and_have_isolated_modes(self):
        for path in (ROOT / 'prodWorkflow').glob('*.yaml'):
            if path.name == 'workflow.yaml':
                continue
            normal, test = config.load(path), config.load(path, True)
            self.assertNotEqual(normal['step_dir'], test['step_dir'])
            self.assertNotEqual(normal['output_dir'], test['output_dir'])

    def test_unknown_settings_and_reserved_options_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown settings'):
            self.settings(typo=True)
        with self.assertRaisesRegex(ValueError, 'reserved'):
            planning.plan(self.settings(options={'--inputfile': 'somewhere'}))
        with self.assertRaisesRegex(ValueError, 'Only disgen'):
            self.settings('analysis', input_dir=str(self.inputs))

    def test_ranges_are_bounded_and_use_checked_counts(self):
        settings = self.settings()
        self.assertEqual(planning.ranges(settings, 10), [(0, 4), (4, 4), (8, 2)])
        self.assertEqual(planning.ranges(settings, 6), [(0, 4), (4, 2)])
        self.assertEqual(planning.ranges(dict(settings, events_per_subjob=None, n_subjobs=3), 10), [(0, 4), (4, 3), (7, 3)])
        self.assertEqual(planning.ranges(dict(settings, test=True, step='disreplay_gen'), 10), [(0, 2)])
        self.assertEqual(planning.ranges(settings, 0), [(0, 0)])

    def test_downstream_uses_export_only_without_root_reads(self):
        source = self.record(entries=7)
        self.exported('disgen', [source])
        settings = self.settings('disreplay_gen', events_per_subjob=3)
        with patch.object(checking.preparation, 'root_statistics', side_effect=AssertionError('ROOT opened during planning')):
            groups = planning.plan(settings)
        self.assertEqual(len(groups), 1)
        self.assertEqual([unit['spec']['count'] for unit in groups[0]['units']], [3, 3, 1])
        self.assertEqual(groups[0]['inputs']['sources'], source['sources'])

    def test_incomplete_and_stale_exports_block_by_default(self):
        upstream = self.exported('disgen', [self.record()], complete=False)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            planning.plan(self.settings('disreplay_gen'))
        self.assertEqual(len(planning.plan(self.settings('disreplay_gen', require_complete=False))), 1)
        upstream.data['revision'] += 1
        upstream.save()
        with self.assertRaisesRegex(ValueError, 'stale'):
            planning.plan(self.settings('disreplay_gen', require_complete=False))

    def test_changed_checked_input_blocks_submission_planning(self):
        record = self.record()
        self.exported('disgen', [record])
        Path(record['sources'][0]).write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError, 'Checked file changed'):
            planning.plan(self.settings('disreplay_gen'))

    def test_saved_physics_frozen_resources_can_change(self):
        saved = self.prepared()
        saved.save()
        changed = dict(saved.settings, memory_mb=9000, n_jobs=1)
        state.State(changed)
        with self.assertRaisesRegex(ValueError, 'Settings differ'):
            state.State(dict(changed, first_event=1))

    def test_submit_records_job_before_backend_and_sets_resources(self):
        saved, api = self.submitted(self.settings())
        job = api.registry[100]
        self.assertEqual(len(job.splitter.args), 3)
        self.assertEqual(job.outputfiles[0].name, '*_disgen.root')
        self.assertEqual(job.backend.cdf_options['request_disk'], '50000MB')
        self.assertEqual(saved.data['attempts'][0]['phase'], 'submitted')
        self.assertEqual(planning.select(saved), [])

    def test_submit_exception_keeps_uncertain_attempt_nonretryable(self):
        saved = self.prepared()
        with self.assertRaisesRegex(RuntimeError, 'backend interrupted'), contextlib.redirect_stdout(io.StringIO()):
            submission.submit(saved, planning.select(saved), self.api(fail=True))
        self.assertEqual(json.loads(saved.path.read_text())['attempts'][0]['phase'], 'uncertain')
        self.assertEqual(planning.select(saved, retry=True), [])

    def test_complete_replay_master_required_and_only_failed_range_retried(self):
        self.exported('disgen', [self.record(entries=6)])
        saved, api = self.submitted(self.settings('disreplay_gen', events_per_subjob=3))
        units = saved.data['groups'][0]['units']
        failed = units[1]['id']
        self.replay_logs(saved, omit=[failed])
        self.assertEqual(self.check(saved), 1)
        self.assertEqual(saved.checked()['records'], [])
        saved.data['attempts'][0]['statuses'] = {units[0]['id']: 'completed', failed: 'failed'}
        repairs = planning.select(saved, retry=True)
        self.assertEqual([unit['id'] for unit in repairs[0]['units']], [failed])
        old_paths = deepcopy(saved.data['attempts'][0]['outputs'][failed])
        with contextlib.redirect_stdout(io.StringIO()):
            submission.submit(saved, repairs, api)
        self.assertNotEqual(saved.data['attempts'][1]['outputs'][failed], old_paths)
        self.assertEqual(saved.data['attempts'][1]['units'][failed], None)
        self.assertEqual(saved.data['attempts'][1]['job_id'], 107)
        self.replay_logs(saved)
        self.assertEqual(self.check(saved), 0)
        export = saved.checked()['records']
        self.assertEqual(len(export), 1)
        self.assertEqual(len(export[0]['sources']), 2)
        self.assertEqual(export[0]['entries'], 6)
        self.assertEqual(planning.select(saved, retry=True), [])

    def test_active_old_attempt_blocks_retry(self):
        saved, api = self.submitted(self.settings())
        first = saved.data['groups'][0]['units'][0]['id']
        saved.data['attempts'][0]['statuses'][first] = 'running'
        self.assertEqual(saved.status(first), 'running')
        self.assertEqual(planning.select(saved, retry=True), [])

    def test_successful_checks_cache_metadata_until_files_change(self):
        self.exported('disgen', [self.record(entries=6)])
        saved, _ = self.submitted(self.settings('disreplay_gen', events_per_subjob=3))
        self.replay_logs(saved)
        self.assertEqual(self.check(saved), 0)
        with patch.object(checking.preparation, 'root_statistics', side_effect=AssertionError('cached metadata reread')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved, metadata=True), 0)
        with patch.object(checking.preparation, 'root_statistics', side_effect=self.metadata) as metadata, contextlib.redirect_stdout(io.StringIO()):
            checking.check(saved, refresh=True)
        self.assertEqual(metadata.call_count, 2)

    def test_foreign_transfer_and_bad_metadata_not_exported(self):
        self.exported('disgen', [self.record(entries=6)])
        saved, _ = self.submitted(self.settings('disreplay_gen'))
        self.replay_logs(saved)
        with patch.object(checking.preparation, 'root_statistics', return_value=('incomplete preparation metadata', None)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved), 1)
        self.assertEqual(saved.checked()['records'], [])
        transfer = self.workspace / '100/output/__postprocesslocations__'
        transfer.write_text(transfer.read_text() + 'massstorage *.root /tmp/foreign.root\n')
        self.assertEqual(self.check(saved), 1)
        self.assertIn('Transferred files differ', saved.checked()['problems'][0]['reason'])

    def test_replayfilter_whole_master_and_analysis_per_filtered_master(self):
        record = self.record(name='sim_a_disreplay_gen.root')
        geometry = self.base / 'geo_a_disreplay_gen.root'
        geometry.write_bytes(b'geometry')
        record.update(master=str(self.base / 'prepared.root'), replay_type='disreplay_gen', geometries=[str(geometry)])
        record['files'].append(state.fingerprint(geometry))
        self.exported('disreplay_gen', [record])
        groups = planning.plan(self.settings('replayfilter'))
        self.assertEqual(groups[0]['units'][0]['spec']['filter']['sources'], record['sources'])
        with self.assertRaisesRegex(ValueError, 'whole checked replay masters'):
            planning.plan(self.settings('replayfilter', events_per_subjob=1))
        record = self.record(name='muonDis_a_disreplay_gen_filtered.root')
        record['replay_type'] = 'disreplay_gen'
        self.exported('replayfilter', [record])
        planned = planning.plan(self.settings('analysis'))
        spec = planned[0]['units'][0]['spec']
        self.assertEqual(spec['analysis']['type'], 'disreplay_gen')
        self.assertFalse(spec['analysis']['save_plots'])
        scratch = self.base / 'scratch'
        scratch.mkdir()
        cwd = Path.cwd()
        try:
            os.chdir(scratch)
            with contextlib.redirect_stdout(io.StringIO()), patch.object(worker.subprocess, 'run', side_effect=AssertionError('analysis executed')):
                worker.run(spec, prepare_only=True)
            self.assertTrue((scratch / 'analysis-input/000000/sim_muonDis_a_disreplay_gen.root').is_symlink())
            self.assertTrue((scratch / 'analysis-config.json').is_file())
        finally:
            os.chdir(cwd)

    def test_optional_filter_branch_requires_matching_checker(self):
        self.exported('disgen', [self.record()])
        self.assertEqual(len(planning.plan(self.settings('disfilter'))), 1)
        with self.assertRaisesRegex(ValueError, 'Run check'):
            planning.plan(self.settings('disreplay_filter'))
        self.exported('disfilter', [self.record(name='filtered.root')])
        self.assertEqual(len(planning.plan(self.settings('disreplay_filter'))), 1)

    def test_test_mode_cannot_consume_production_exports(self):
        self.exported('disgen', [self.record()])
        with self.assertRaisesRegex(ValueError, 'Run check'):
            planning.plan(self.settings('disreplay_gen', test=True))

    def test_preview_no_ganga_jobs_or_saved_state(self):
        saved = self.prepared()
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            submission.preview(saved, planning.select(saved), True)
        self.assertIn('Interactive (fresh directory', stream.getvalue())
        self.assertFalse(saved.path.exists())
        self.assertEqual(len(list((saved.path.parent / 'preview').glob('*.json'))), 3)

    def test_cli_rejects_yaml_overrides_and_submission_outside_ganga(self):
        settings = self.settings()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            workflow.main([settings['config_file'], '--n-jobs', '2'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            workflow.main([settings['config_file'], '--submit'])

    def test_root_launcher_help_does_not_start_ganga(self):
        result = subprocess.run(['bash', str(ROOT / 'gangaSubmit.sh'), 'retry', '--help'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('checker-rejected terminal ranges', result.stdout)

    def test_missing_stdout_reports_condor_disk_failure(self):
        saved, _ = self.submitted(self.settings())
        log = self.workspace / '100/0/output/condorLog'
        log.parent.mkdir(parents=True)
        log.write_text('Job removed by SYSTEM_PERIODIC_REMOVE due to disk usage exceeded allowed max\n')
        self.check(saved)
        self.assertIn('disk usage exceeded', saved.checked()['problems'][0]['reason'])

    def test_live_resubmission_does_not_reuse_previous_successful_logs(self):
        self.exported('disgen', [self.record(entries=6)])
        saved, api = self.submitted(self.settings('disreplay_gen'))
        self.replay_logs(saved)
        self.assertEqual(self.check(saved), 0)
        api.registry[100].status = 'running'
        saved.refresh(api.jobs)
        self.assertEqual(self.check(saved), 1)
        self.assertEqual(saved.checked()['records'], [])

    def test_worker_runs_argv_without_shell_and_propagates_failure(self):
        saved = self.prepared()
        spec = saved.data['groups'][0]['units'][0]['spec']
        with patch.object(worker.subprocess, 'run', side_effect=subprocess.CalledProcessError(3, ['python3'])) as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                worker.run(spec)
        call = run.call_args
        self.assertIsInstance(call.args[0], list)
        self.assertIn('-u', call.args[0])
        self.assertIn('faulthandler', call.args[0])
        self.assertNotIn('shell', call.kwargs)
        self.assertEqual(call.kwargs['env']['PYTHONUNBUFFERED'], '1')

    def test_worker_can_be_invoked_from_a_ganga_style_sandbox(self):
        import shutil
        sandbox = self.base / 'sandbox'
        sandbox.mkdir()
        for source in (ROOT / 'prodWorkflow/worker.py', ROOT / 'wn_script_pixi.py'):
            shutil.copyfile(source, sandbox / source.name)
        spec = self.prepared().data['groups'][0]['units'][0]['spec']
        (sandbox / 'task.json').write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-B', str(sandbox / 'worker.py'), 'task.json', '--prepare-only'],
                                cwd=sandbox, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INFO: Executing:', result.stdout)
        self.assertIn('Exit code: 0', result.stdout)

    def test_merge_skips_extracting_events_and_records_exact_archives(self):
        directory = self.base / 'analysis_evt0_3'
        directory.mkdir()
        manifest = dict(output_version=6,
                        normalization=dict(preparation_complete=True, records=[dict(id='prepared-id', **NORMALIZATION['records']['prepared-id'])]),
                        analysis_signature=dict(type='disreplay_gen', sha256='analysis-code'),
                        config=dict(save_plots=False), entries_processed=3, entries_available=3,
                        chain_entry_start=0, chain_entry_stop=3, weight_definition='test',
                        files=['/input.root'], source_ranges=[dict(path='/input.root', file_uuid='file-id', tree='cbmsim', start=0, stop=3, entries=3)])
        (directory / 'run.json').write_text(json.dumps(manifest))
        for name in ('histograms.npz', 'histogram_flows.csv', 'normalization.csv', 'cutflow.csv',
                     'pair_cutflow.csv', 'pair_composition_cutflow.csv', 'tagger_cutflow.csv', 'events.jsonl'):
            (directory / name).write_text('content\n')
        archive = self.base / 'analysis.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            stream.add(directory, arcname=directory.name)
        analysis = checking.checkAnalysis.read_analysis_archive(str(archive))
        analysis['archive'] = str(analysis['archive'])
        record = dict(id='analysis-unit', sources=[str(archive)], files=[state.fingerprint(archive)], entries=3, analysis=analysis)
        self.exported('analysis', [record])
        install = self.base / 'FairShip/newMuonDIS'
        install.mkdir(parents=True)
        (install / 'mergeAnalysis.py').write_text('# test stub; subprocess mocked\n')
        settings = self.settings('merger', fs_install=str(install.parent), merge_output=str(self.base / 'merged'))
        saved = state.State(settings)
        with patch.object(checking.checkAnalysis.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.merge(saved, execute=True), 0)
        extracted = self.base / 'merged_inputs/0/analysis_evt0_3'
        self.assertFalse((extracted / 'events.jsonl').exists())
        self.assertTrue((extracted / 'cutflow.csv').is_file())
        self.assertEqual(json.loads((extracted / 'run.json').read_text())['omitted_files'], ['events.jsonl'])
        self.assertIn('--skip-event-records', run.call_args.args[0])
        self.assertEqual(json.loads(saved.path.read_text())['merge']['inputs'], [str(archive)])

    def test_preview_then_append_new_upstream_batch_preserves_groups(self):
        first = self.record(identity='first')
        upstream = self.exported('disgen', [first])
        settings = self.settings('disreplay_gen', require_complete=False)
        saved = self.prepared(settings)
        before = deepcopy(saved.data['groups'])
        second = self.record(name='prepared2.root', identity='second')
        export = upstream.checked()
        export['records'].append(second)
        state.write_json(upstream.path.with_name('checked.json'), export)
        saved.add_groups(planning.plan(settings))
        self.assertEqual(saved.data['groups'][:1], before)
        self.assertEqual(len(saved.data['groups']), 2)

    def test_disgen_and_disfilter_checkers_save_counts_and_print_stage_summaries(self):
        saved, _ = self.submitted(self.settings(events_per_file=1, events_per_subjob=None))
        attempt = saved.data['attempts'][0]
        unit = saved.data['groups'][0]['units'][0]
        output = Path(attempt['outputs'][unit['id']][0])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'prepared data')
        logdir = self.workspace / '100/output'
        logdir.mkdir(parents=True)
        (logdir / 'stdout').write_text(PREPARE_LOG)
        (logdir / '__postprocesslocations__').write_text(f'massstorage *.root {output}\n')
        with patch.object(checking.preparation, 'root_statistics', return_value=('successful', dict(written=10, normalization=NORMALIZATION))), contextlib.redirect_stdout(io.StringIO()) as report:
            self.assertEqual(checking.check(saved, metadata=True), 0)
        self.assertIn('Muon path', report.getvalue())
        self.assertIn('input_muons=10', report.getvalue())
        self.assertEqual(saved.checked()['records'][0]['entries'], 10)
        filtered, _ = self.submitted(self.settings('disfilter'))
        attempt = filtered.data['attempts'][0]
        unit = filtered.data['groups'][0]['units'][0]
        output = Path(attempt['outputs'][unit['id']][0])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'filtered data')
        (logdir / 'stdout').write_text(FILTER_LOG + 'INFO: Finished running. These files are on the WN:\n')
        (logdir / '__postprocesslocations__').write_text(f'massstorage *.root {output}\n')
        with patch.object(checking.preparation, 'root_statistics', return_value=('successful', dict(written=2, normalization=NORMALIZATION))), contextlib.redirect_stdout(io.StringIO()) as report:
            self.assertEqual(checking.check(filtered), 0)
        self.assertIn('DIS MS weighted', report.getvalue())
        self.assertEqual(filtered.checked()['records'][0]['entries'], 2)

    def test_replayfilter_check_exports_even_zero_selected_entries(self):
        record = self.record(name='sim_a_disreplay_gen.root')
        geometry = self.base / 'geo_a_disreplay_gen.root'
        geometry.write_bytes(b'geometry')
        record.update(master=str(self.base / 'prepared.root'), replay_type='disreplay_gen', geometries=[str(geometry)])
        record['files'].append(state.fingerprint(geometry))
        self.exported('disreplay_gen', [record])
        saved, _ = self.submitted(self.settings('replayfilter'))
        unit = saved.data['groups'][0]['units'][0]
        output, geo, manifest_path = map(Path, saved.data['attempts'][0]['outputs'][unit['id']])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'ROOT with zero selected entries')
        geo.write_bytes(b'geometry')
        counts = dict(source_entry_start=0, source_entry_stop=10, entries_available=10,
                      events_processed=10, events_two_tracks=0, events_selected=0)
        payload = dict(version=2, production=unit['spec']['filter'],
                       filter=dict(counts, sources=[dict(counts, source_uuid='source-id', source_file=record['sources'][0])]),
                       output=output.name, geometry=geo.name, output_bytes=output.stat().st_size,
                       geometry_bytes=geo.stat().st_size)
        manifest_path.write_text(json.dumps(payload))
        logdir = self.workspace / '100/output'
        logdir.mkdir(parents=True)
        (logdir / 'stdout').write_text(f'Replayfilter manifest: {manifest_path.name}\nINFO: Finished running.\nExit code: 0\n')
        (logdir / '__postprocesslocations__').write_text(''.join(f'massstorage *_filtered.* {path}\n' for path in (output, geo, manifest_path)))
        with patch.object(checking.preparation, 'root_statistics', return_value=('successful', dict(written=0, normalization=NORMALIZATION))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved), 0)
        exported = saved.checked()['records'][0]
        self.assertEqual(exported['entries'], 0)
        analysis = planning.plan(self.settings('analysis'))
        self.assertEqual(analysis[0]['units'][0]['spec']['count'], 0)


if __name__ == '__main__':
    unittest.main()
