"""No ROOT/EOS/Condor required: exercise planning, checking and Ganga boundaries."""

import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
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
                    command = ['python3', str(Path(spec['fs_install']) / spec['runfile']), *spec['args']]
                    log = 'INFO: Executing: ' + shlex.join(command) + '\n' + log
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
            if path.name == 'workflow.yaml' or path.name.startswith('workflow_'):
                continue
            normal, test = config.load(path), config.load(path, True)
            self.assertNotEqual(normal['step_dir'], test['step_dir'])
            self.assertNotEqual(normal['output_dir'], test['output_dir'])

    def test_shared_geometry_reaches_each_stage(self):
        geometry = str(self.base / 'shared.root')
        common = dict(self.common, geometry_file=geometry)
        (self.base / 'workflow.yaml').write_text(yaml.safe_dump(common))
        for step, flag in (('disgen', '--geoFile'), ('disfilter', '--geoFile'),
                           ('disreplay_gen', '--geofile'), ('disreplay_filter', '--geofile')):
            settings = self.settings(step)
            self.assertEqual(settings['geometry_file'], geometry)
            self.assertEqual(settings['options'][flag], geometry)
            self.assertIn(geometry, planning.executable_options(settings))
        self.assertEqual(self.settings('replayfilter')['geometry_file'], geometry)
        self.assertEqual(self.settings('analysis')['analysis']['geometry_file'], geometry)
        self.assertEqual(self.settings('merger')['geometry_file'], geometry)
        override = self.settings('analysis', geometry_file='other.root')
        self.assertEqual(override['analysis']['geometry_file'], str(self.base / 'other.root'))
        with self.assertRaisesRegex(ValueError, 'Use geometry_file'):
            self.settings(options={'--geoFile': geometry})

    def test_existing_analysis_can_be_inspected_after_shared_geometry_is_added(self):
        old = self.settings('analysis')
        state.State(old).save()
        common = dict(self.common, geometry_file=str(self.base / 'shared.root'))
        (self.base / 'workflow.yaml').write_text(yaml.safe_dump(common))
        updated = self.settings('analysis')
        state.State(updated, inspect_existing=True)
        with self.assertRaisesRegex(ValueError, 'Settings differ'):
            state.State(updated)

    def test_parallel_campaign_stages_share_their_own_namespace(self):
        folder = ROOT / 'prodWorkflow'
        for test in (False, True):
            original = config.load(folder / 'disgen.yaml', test)
            parallel = config.load(folder / 'disgen_26.yaml', test)
            self.assertNotEqual(original['campaign_dir'], parallel['campaign_dir'])
            for step in config.STEPS:
                settings = config.load(folder / f'{step}_26.yaml', test)
                self.assertEqual(settings['campaign_dir'], parallel['campaign_dir'])
                self.assertEqual(Path(settings['output_dir']).parent,
                                 Path(parallel['output_dir']).parent)

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

    def test_one_input_creates_only_25_ranges_and_does_not_fingerprint_the_rest(self):
        (self.inputs / 'sim_b.root').touch()
        settings = self.settings(n_jobs=1, events_per_file=250000, events_per_subjob=10000)
        with patch.object(planning, 'fingerprint', wraps=state.fingerprint) as fingerprint:
            groups = planning.plan(settings)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]['units']), 25)
        self.assertEqual([(unit['spec']['first'], unit['spec']['count']) for unit in groups[0]['units']],
                         [(first, 10000) for first in range(0, 250000, 10000)])
        fingerprint.assert_called_once_with(str(self.inputs / 'sim_a.root'))

    def test_limited_cli_submissions_record_only_each_requested_batch(self):
        (self.inputs / 'sim_b.root').write_bytes(b'next input')
        settings = self.settings(n_jobs=1, events_per_file=250000, events_per_subjob=10000)
        self.current_state = state.State(settings)
        api = self.api()
        args = ['plan', settings['config_file'], '--submit']
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.main(args, api)
        saved = state.State(settings)
        self.assertEqual(len(saved.data['groups']), 1)
        self.assertEqual(len(saved.data['attempts']), 1)
        self.assertEqual(len(list(saved.path.parent.glob('tasks/*/*.json'))), 25)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(planning, 'fingerprint', wraps=state.fingerprint) as fingerprint:
            workflow.main(args, api)
        fingerprint.assert_called_once_with(str(self.inputs / 'sim_b.root'))
        saved = state.State(settings)
        self.assertEqual(len(saved.data['groups']), 2)
        self.assertEqual(len(saved.data['attempts']), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.main(args, api)
        self.assertEqual(len(state.State(settings).data['attempts']), 2)

    def test_limited_downstream_plan_does_not_verify_unselected_inputs(self):
        first, second = self.record(), self.record(name='other.root', identity='other')
        self.exported('disgen', [first, second])
        Path(second['sources'][0]).unlink()
        with patch.object(planning, 'verify', wraps=state.verify) as verify:
            groups = planning.plan(self.settings('disreplay_gen', n_jobs=1))
        self.assertEqual([group['inputs']['id'] for group in groups], [first['id']])
        verify.assert_called_once_with(first['files'])

    def test_test_mode_stays_on_first_input_without_fingerprinting_the_rest(self):
        (self.inputs / 'sim_b.root').touch()
        settings = self.settings(test=True)
        saved, _ = self.submitted(settings)
        self.assertEqual(planning.plan(settings, saved), [])

    def test_check_prunes_legacy_groups_without_attempts_but_keeps_partial_masters(self):
        (self.inputs / 'sim_b.root').write_bytes(b'unrequested input')
        settings = self.settings(n_jobs=1)
        saved = state.State(settings)
        saved.add_groups(planning.plan(dict(settings, n_jobs=-1)))
        self.current_state = saved
        with contextlib.redirect_stdout(io.StringIO()):
            submission.submit(saved, planning.select(saved), self.api())
        # Keep missing ranges within a master that has at least one attempt.
        first_unit = saved.data['groups'][0]['units'][0]['id']
        saved.data['attempts'][0]['units'] = {first_unit: 0}
        saved.save()
        previous_revision = saved.data['revision']
        state.write_json(saved.path.with_name('checked.json'),
                         dict(state_id=saved.data['id'], revision=previous_revision, successful=[], problems=[]))
        with patch.object(checking, 'inspect', side_effect=ValueError('expected failure')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(['check', settings['config_file']]), 1)
        compact = state.State(settings)
        self.assertEqual(len(compact.data['groups']), 1)
        self.assertEqual(compact.data['groups'][0], saved.data['groups'][0])
        self.assertEqual(compact.data['attempts'], saved.data['attempts'])
        self.assertGreater(compact.data['revision'], previous_revision)
        self.assertEqual(len(compact.checked()['problems']), 3)
        self.assertNotIn(saved.data['groups'][1]['units'][0]['id'], output.getvalue())

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

    def test_legacy_replayfilter_diagnostics_can_be_inspected_without_migration(self):
        settings = self.settings('replayfilter')
        saved = state.State(settings)
        saved.data['configuration'].pop('diagnostics')
        saved.save()
        original = saved.path.read_bytes()
        inspected = state.State(settings, inspect_existing=True)
        self.assertEqual(inspected.data, saved.data)
        self.assertEqual(saved.path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'Settings differ'):
            state.State(settings)
        for change in (dict(cross_section='analytical'), dict(geometry_file='/different.root'),
                       dict(first_event=1)):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Settings differ'):
                state.State(dict(settings, **change), inspect_existing=True)

    def test_recorded_diagnostics_changes_still_rejected_when_inspecting(self):
        settings = self.settings('replayfilter', diagnostics=False)
        state.State(settings).save()
        with self.assertRaisesRegex(ValueError, 'Settings differ'):
            state.State(dict(settings, diagnostics=True), inspect_existing=True)

    def test_legacy_replayfilter_check_handoff_and_status(self):
        settings = self.settings('replayfilter')
        saved = state.State(settings)
        saved.data['configuration'].pop('diagnostics')
        saved.data['groups'] = [dict(id='group', units=[dict(id='unit')])]
        saved.data['attempts'] = [dict(id='attempt', job_id=100, phase='submitted',
                                      units={'unit': None}, statuses={'unit': 'completed'})]
        saved.save()
        with patch.object(checking, 'check', return_value=0) as check:
            self.assertEqual(workflow.main(['check', settings['config_file'], '--metadata']), 0)
        self.assertEqual(check.call_args.args[0].data['configuration'], saved.data['configuration'])
        self.assertEqual(check.call_args.args[1:], (True, False, False))
        argv = ['check', settings['config_file'], '--metadata']
        with patch.object(workflow, 'fairship', return_value=0) as fairship:
            self.assertEqual(workflow.main(argv, self.api()), 0)
        self.assertEqual(fairship.call_args.args[1], argv)
        self.assertEqual(json.loads(saved.path.read_text())['configuration'], saved.data['configuration'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['status', settings['config_file']]), 0)
        for action in ('plan', 'retry'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'Settings differ'):
                workflow.main([action, settings['config_file']])

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

    def test_disgen_seed_pairing_is_unique_and_bounded(self):
        self.assertEqual(submission.disgen_seed('261112', 100, 0), 266163)
        self.assertEqual(submission.disgen_seed('261112', 100, 1), 266265)
        self.assertNotEqual(submission.disgen_seed('261112', 100, 1),
                            submission.disgen_seed('261112', 101, 0))
        for production in ('261112', 'unit_test', '899999998'):
            seeds = {submission.disgen_seed(production, master, child)
                     for master in range(100) for child in range(100)}
            self.assertEqual(len(seeds), 10000)
            self.assertTrue(all(1 <= seed <= 899999999 for seed in seeds))
        # Invert the pairing at the largest supported value and just beyond it.
        from math import isqrt
        for pair in (899999998, 899999999):
            diagonal = (isqrt(8 * pair + 1) - 1) // 2
            child = pair - diagonal * (diagonal + 1) // 2
            master = diagonal - child
            if pair == 899999998:
                self.assertEqual(submission.disgen_seed('0', master, child), 899999999)
            else:
                with self.assertRaisesRegex(ValueError, 'collision-free'):
                    submission.disgen_seed('0', master, child)
        for master, child in ((-1, 0), (0, -1), (True, 0)):
            with self.assertRaisesRegex(ValueError, 'nonnegative integers'):
                submission.disgen_seed('261112', master, child)

    def test_disgen_submission_saves_and_logs_each_seed(self):
        saved, api = self.submitted(self.settings(production='261112'))
        tasks = [json.loads(Path(path).read_text()) for path in api.registry[100].inputfiles
                 if path.endswith('.json')]
        self.assertEqual([task['random_seed']['value'] for task in tasks], [266163, 266265, 266368])
        for child, spec in enumerate(tasks):
            self.assertEqual(spec['args'][-2:], ['--seed', str(spec['random_seed']['value'])])
            self.assertEqual(spec['random_seed']['master_job'], 100)
            self.assertEqual(spec['random_seed']['subjob'], child)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(worker.subprocess, 'run') as run:
                worker.run(spec, prepare_only=True)
            run.assert_not_called()
            self.assertIn(f'Random seed: {spec["random_seed"]["value"]} '
                          f'(production=261112, master=100, subjob={child})', output.getvalue())
        # Attempt-specific seeds must not leak into the reusable plan.
        for unit in saved.data['groups'][0]['units']:
            self.assertNotIn('--seed', unit['spec']['args'])
        # Retrying a single range creates an unsplit master: its subjob index is 0.
        group = saved.data['groups'][0]
        repair = dict(group, units=[group['units'][1]])
        with contextlib.redirect_stdout(io.StringIO()):
            submission.submit(saved, [repair], api)
        path = next(path for path in api.registry[107].inputfiles if path.endswith('.json'))
        retry = json.loads(Path(path).read_text())
        self.assertEqual(retry['random_seed'], dict(value=266891, production='261112', master_job=107, subjob=0))
        self.assertEqual(retry['args'].count('--seed'), 1)
        # The original submitted task remains a reproducible record.
        self.assertEqual(json.loads(Path(api.registry[100].inputfiles[1]).read_text()), tasks[1])

    def test_disgen_seed_overrides_and_other_stages_are_preserved(self):
        for value in (12345, None):
            saved, api = self.submitted(self.settings(name=f'override_{value}', options={'--seed': value}))
            spec = json.loads(Path(api.registry[100].inputfiles[0]).read_text())
            self.assertNotIn('random_seed', spec)
            self.assertEqual(spec['args'].count('--seed'), int(value is not None))
            if value is not None:
                self.assertEqual(spec['args'][spec['args'].index('--seed') + 1], str(value))
        self.exported('disgen', [self.record()])
        saved, api = self.submitted(self.settings('disreplay_gen'))
        spec = json.loads(Path(api.registry[100].inputfiles[0]).read_text())
        self.assertNotIn('random_seed', spec)
        self.assertEqual(spec['args'][spec['args'].index('--seed') + 1], '0')

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

    def test_replay_input_muons_can_generate_more_dis_events(self):
        self.exported('disgen', [self.record(entries=2)])
        saved, _ = self.submitted(self.settings('disreplay_gen', events_per_file=2))
        self.replay_logs(saved)
        logfile = self.workspace / '100/output/stdout'
        log = logfile.read_text().replace('Generating 2 DIS events', 'Generating 500 DIS events')
        log = log.replace("'TimeDetPoint': 0}", "'TimeDetPoint': 0, 'UpstreamTaggerScoringPoint': 480}")
        logfile.write_text(log)
        with patch.object(checking.preparation, 'root_statistics',
                          return_value=('successful', dict(written=500, normalization=deepcopy(NORMALIZATION)))), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(checking.check(saved), 0)
        self.assertEqual(saved.checked()['records'][0]['entries'], 500)
        self.assertIn('Restored UpstreamTaggerScoringPoint', output.getvalue())
        # The generated DIS count must still agree with the output tree.
        with patch.object(checking.preparation, 'root_statistics',
                          return_value=('successful', dict(written=499, normalization=deepcopy(NORMALIZATION)))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved, refresh=True), 1)
        self.assertIn('ROOT entries differ', saved.checked()['problems'][0]['reason'])
        for changed in (log.replace('-n 2', '-n 3'), log.replace('-i 0', '-i 1'),
                        log.replace('first event 0', 'first event 1'),
                        '\n'.join(log.splitlines()[1:]), log.splitlines()[0] + '\n' + log):
            with self.subTest(log=changed):
                logfile.write_text(changed)
                self.assertEqual(self.check(saved), 1)
                self.assertIn('range differs', saved.checked()['problems'][0]['reason'])

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

    def test_old_checker_cache_and_full_replay_check_revalidate(self):
        self.exported('disgen', [self.record(entries=6)])
        saved, _ = self.submitted(self.settings('disreplay_gen', events_per_subjob=3))
        self.replay_logs(saved)
        self.assertEqual(self.check(saved), 0)
        record = saved.checked()
        record.pop('check_version')
        state.write_json(saved.path.with_name('checked.json'), record)
        with patch.object(checking.preparation, 'root_statistics', side_effect=self.metadata) as read, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved), 0)
        self.assertEqual(read.call_count, 2)
        with patch.object(checking.preparation, 'root_statistics', side_effect=self.metadata) as read, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checking.check(saved, full=True), 0)
        self.assertTrue(all(call.kwargs['replay_paths'] for call in read.call_args_list))

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
        self.assertTrue(groups[0]['units'][0]['spec']['filter']['diagnostics'])
        disabled = planning.plan(self.settings('replayfilter', diagnostics=False))
        self.assertFalse(disabled[0]['units'][0]['spec']['filter']['diagnostics'])
        for mode in ('stored', 'analytical'):
            settings = self.settings('replayfilter', cross_section=mode)
            self.assertEqual(planning.plan(settings)[0]['units'][0]['spec']['filter']['cross_section'], mode)
        for mode in ('constant', 'invalid', None, True):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'cross_section'):
                self.settings('replayfilter', cross_section=mode)
        with self.assertRaisesRegex(ValueError, 'only to replayfilter'):
            self.settings('disreplay_gen', cross_section='analytical')
        with self.assertRaisesRegex(ValueError, 'one unsplit chain'):
            planning.plan(self.settings('replayfilter', events_per_subjob=1))
        record = self.record(name='muonDis_a_disreplay_gen_filtered.root')
        record['replay_type'] = 'disreplay_gen'
        self.exported('replayfilter', [record])
        planned = planning.plan(self.settings('analysis'))
        spec = planned[0]['units'][0]['spec']
        self.assertEqual(spec['analysis']['type'], 'disreplay_gen')
        self.assertFalse(spec['analysis']['save_plots'])
        self.assertTrue(spec['analysis']['tree_only'])
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

    def analysis_records(self, counts=(3, 5, 0)):
        records = []
        for index, entries in enumerate(counts):
            record = self.record(entries=entries, identity=f'filter-{index}',
                                 name=f'muonDis_{index}_disreplay_gen_filtered.root')
            geometry = Path(record['sources'][0]).with_name('geo_' + Path(record['sources'][0]).name)
            geometry.write_bytes(b'geometry')
            record.update(replay_type='disreplay_gen', geometries=[str(geometry)])
            record['files'].append(state.fingerprint(geometry))
            records.append(record)
        return records

    def test_analysis_grouping_configuration_and_existing_state(self):
        for value in (0, -2, True, 1.5, None, 'all'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'masters_per_job'):
                self.settings('analysis', masters_per_job=value)
        with self.assertRaisesRegex(ValueError, 'applies only to analysis'):
            self.settings('replayfilter', masters_per_job=2)
        self.assertEqual(self.settings('analysis')['masters_per_job'], -1)
        settings = self.settings('analysis', masters_per_job=1)
        saved = state.State(settings)
        self.assertNotIn('masters_per_job', saved.data['configuration'])
        saved.save()
        self.assertEqual(state.State(self.settings('analysis', masters_per_job=1)).data['id'], saved.data['id'])
        with self.assertRaisesRegex(ValueError, 'Settings differ'):
            state.State(self.settings('analysis', masters_per_job=-1))

    def test_analysis_cross_section_is_validated_and_passed_to_worker(self):
        self.exported('replayfilter', self.analysis_records())
        self.assertNotIn('cross_section', self.settings('analysis')['analysis'])
        for mode in ('constant', 'analytical', 'stored'):
            settings = self.settings('analysis', analysis=dict(cross_section=mode))
            spec = planning.plan(settings)[0]['units'][0]['spec']
            self.assertEqual(spec['analysis']['cross_section'], mode)
        for mode in ('invalid', None, True):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'analysis.cross_section'):
                self.settings('analysis', analysis=dict(cross_section=mode))

    def test_analysis_all_masters_submit_one_job_and_stage_every_input(self):
        records = self.analysis_records()
        self.exported('replayfilter', records)
        saved, api = self.submitted(self.settings('analysis', n_jobs=1))
        self.assertEqual(len(api.registry), 1)
        job = next(iter(api.registry.values()))
        self.assertFalse(hasattr(job, 'splitter'))
        group, = saved.data['groups']
        unit, = group['units']
        spec = unit['spec']
        self.assertEqual(group['inputs']['parents'], [record['id'] for record in records])
        self.assertEqual(spec['sources'], [record['sources'][0] for record in records])
        self.assertEqual(spec['files'], [item for record in records for item in record['files']])
        self.assertEqual((spec['first'], spec['count']), (0, 8))
        scratch = self.base / 'scratch'
        scratch.mkdir()
        cwd = Path.cwd()
        try:
            os.chdir(scratch)
            with contextlib.redirect_stdout(io.StringIO()), patch.object(worker.subprocess, 'run', side_effect=AssertionError('analysis executed')):
                worker.run(spec, prepare_only=True)
            for index, record in enumerate(records):
                folder = scratch / 'analysis-input' / f'{index:06d}'
                self.assertEqual(next(folder.glob('sim_*.root')).resolve(), Path(record['sources'][0]))
                self.assertEqual(next(folder.glob('geo_*.root')).resolve(), Path(record['geometries'][0]))
        finally:
            os.chdir(cwd)
        self.assertEqual(planning.plan(saved.settings, saved), [])

    def test_analysis_grouping_batches_and_chain_ranges(self):
        records = self.analysis_records((3, 5, 7))
        self.exported('replayfilter', list(reversed(records)))
        groups = planning.plan(self.settings('analysis', masters_per_job=2, first_event=2,
                                             events_per_file=5, events_per_subjob=3))
        self.assertEqual([group['inputs']['parents'] for group in groups], [['filter-0', 'filter-1'], ['filter-2']])
        for group in groups:
            self.assertEqual([(unit['spec']['first'], unit['spec']['count']) for unit in group['units']], [(2, 3), (5, 2)])
        groups = planning.plan(self.settings('analysis', masters_per_job=-1, n_subjobs=2))
        self.assertEqual([unit['spec']['count'] for unit in groups[0]['units']], [8, 7])

    def test_analysis_grouping_new_inputs_do_not_repeat_assigned_masters(self):
        records = self.analysis_records((3, 5, 7))
        upstream = self.exported('replayfilter', records[:2], complete=False)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            planning.plan(self.settings('analysis', masters_per_job=-1))
        saved, _ = self.submitted(self.settings('analysis', masters_per_job=-1, require_complete=False))
        original = deepcopy(saved.data['groups'])
        report = upstream.checked()
        report['records'] = list(reversed(records))
        state.write_json(upstream.path.with_name('checked.json'), report)
        groups = planning.plan(saved.settings, saved)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['inputs']['parents'], ['filter-2'])
        self.assertEqual(groups[0]['units'][0]['spec']['count'], 7)
        saved.add_groups(groups)
        self.assertEqual(saved.data['groups'][:1], original)
        self.assertEqual(planning.plan(saved.settings, saved), [])

    def test_analysis_rerun_groups_successful_and_new_masters_together(self):
        self.analysis_rerun(['rerun'], [])

    def test_analysis_rerun_option_groups_successful_and_new_masters_together(self):
        self.analysis_rerun([], ['--rerun'])

    def analysis_rerun(self, prefix, options):
        records = self.analysis_records((3, 5, 7))
        upstream = self.exported('replayfilter', records[:2])
        saved, api = self.submitted(self.settings('analysis', masters_per_job=1))
        for attempt in saved.data['attempts']:
            attempt['statuses'] = dict.fromkeys(attempt['units'], 'completed')
            api.registry[attempt['job_id']].status = 'completed'
        saved.save()
        state.write_json(saved.path.with_name('checked.json'),
                         dict(state_id=saved.data['id'], revision=saved.data['revision'],
                              successful=[dict(unit=unit) for attempt in saved.data['attempts'] for unit in attempt['units']],
                              problems=[]))
        self.assertEqual(planning.select(saved), [])
        report = upstream.checked()
        report['records'] = records
        state.write_json(upstream.path.with_name('checked.json'), report)
        settings = self.settings('analysis')
        args = [*prefix, settings['config_file'], *options]
        checked = saved.path.with_name('checked.json')
        before = {path: path.read_bytes() for path in (saved.path, checked, upstream.path,
                                                     upstream.path.with_name('checked.json'))}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(args, api), 0)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(api.registry), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main([*args, '--submit'], api), 0)
        current = state.State(settings)
        self.assertNotEqual(current.data['id'], saved.data['id'])
        self.assertFalse(checked.exists())
        for path in (upstream.path, upstream.path.with_name('checked.json')):
            self.assertEqual(path.read_bytes(), before[path])
        group, = current.data['groups']
        unit, = group['units']
        self.assertEqual(group['inputs']['parents'], [record['id'] for record in records])
        self.assertEqual(unit['spec']['count'], 15)
        self.assertEqual(len(current.data['attempts']), 1)
        self.assertEqual(len(api.registry), 3)
        self.assertFalse(hasattr(api.registry[114], 'splitter'))

    def test_analysis_grouping_test_mode_and_empty_inputs(self):
        records = self.analysis_records((250, 500))
        self.exported('replayfilter', records, test=True)
        groups = planning.plan(self.settings('analysis', masters_per_job=-1, test=True))
        spec = groups[0]['units'][0]['spec']
        self.assertEqual(spec['sources'], records[0]['sources'])
        self.assertEqual(spec['count'], 100)
        self.exported('replayfilter', self.analysis_records((0, 0)))
        groups = planning.plan(self.settings('analysis', masters_per_job=-1))
        self.assertEqual(len(groups[0]['units']), 1)
        self.assertEqual(groups[0]['units'][0]['spec']['count'], 0)

    def test_analysis_grouping_rejects_mixed_duplicate_and_changed_inputs(self):
        records = self.analysis_records()
        records[1]['replay_type'] = 'disreplay_filter'
        self.exported('replayfilter', records)
        settings = self.settings('analysis', masters_per_job=-1)
        with self.assertRaisesRegex(ValueError, 'different replay types'):
            planning.plan(settings)
        records[1]['replay_type'] = 'disreplay_gen'
        duplicate = dict(records[0], id='duplicate')
        self.exported('replayfilter', [*records, duplicate])
        with self.assertRaisesRegex(ValueError, 'Duplicate analysis source'):
            planning.plan(settings)
        self.exported('replayfilter', records)
        Path(records[-1]['sources'][0]).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Checked file changed'):
            planning.plan(settings)

    def test_replayfilter_entry_limit_applies_once_to_the_chain(self):
        for entries, test, expected in ((25000, False, 10000), (7000, False, 7000),
                                        (0, False, 0), (25000, True, 100)):
            with self.subTest(entries=entries, test=test):
                record = self.record(entries=entries, name='sim_a_disreplay_gen.root')
                other = self.record(name='sim_b_disreplay_gen.root')
                record['sources'].extend(other['sources'])
                record['files'].extend(other['files'])
                record.update(master=str(self.base / 'prepared.root'), replay_type='disreplay_gen',
                              geometries=['geo_a.root', 'geo_b.root'])
                self.exported('disreplay_gen', [record], test=test)
                settings = self.settings('replayfilter', test=test, events_per_file=10000)
                groups = planning.plan(settings)
                self.assertEqual(len(groups), 1)
                self.assertEqual(len(groups[0]['units']), 1)
                spec = groups[0]['units'][0]['spec']
                self.assertEqual((spec['first'], spec['count']), (0, expected))
                self.assertEqual(spec['args'][:4], ['-s', '0', '-n', str(expected)])
                self.assertEqual(spec['filter']['sources'], record['sources'])
                for invalid in (dict(first_event=1), dict(n_subjobs=2), dict(events_per_subjob=100)):
                    with self.assertRaisesRegex(ValueError, 'one unsplit chain'):
                        planning.plan(dict(settings, **invalid))

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

    def test_rerun_replaces_only_selected_step_and_preserves_previous_outputs(self):
        record = self.record(name='replay.root')
        geometry = self.base / 'geometry.root'
        geometry.write_bytes(b'geometry')
        record.update(master=str(self.base / 'prepared.root'), replay_type='disreplay_gen',
                      geometries=[str(geometry)])
        upstream = self.exported('disreplay_gen', [record])
        downstream = self.exported('analysis', [])
        test_state = self.exported('replayfilter', [], test=True)
        saved, api = self.submitted(self.settings('replayfilter'))
        old_attempt = saved.data['attempts'][0]
        old_attempt['statuses'] = dict.fromkeys(old_attempt['units'], 'completed')
        api.registry[old_attempt['job_id']].status = 'completed'
        saved.save()
        state.write_json(saved.path.with_name('checked.json'),
                         dict(state_id=saved.data['id'], revision=saved.data['revision'],
                              successful=[dict(unit=unit) for unit in old_attempt['units']], problems=[]))
        self.assertEqual(planning.select(saved), [])
        old_outputs = [Path(path) for paths in old_attempt['outputs'].values() for path in paths]
        for path in old_outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'previous output')
        protected = [path for other in (upstream, downstream, test_state)
                     for path in (other.path, other.path.with_name('checked.json'))]
        protected += list(saved.path.parent.glob('tasks/*/*.json')) + old_outputs
        before = {path: path.read_bytes() for path in protected}
        api.monitor = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['rerun', saved.settings['config_file'], '--submit'], api), 0)
        current = state.State(saved.settings)
        self.assertNotEqual(current.data['id'], saved.data['id'])
        self.assertEqual(current.data['groups'], saved.data['groups'])
        self.assertEqual(len(current.data['attempts']), 1)
        self.assertNotEqual(current.data['attempts'][0]['id'], old_attempt['id'])
        self.assertNotEqual(current.data['attempts'][0]['outputs'], old_attempt['outputs'])
        self.assertFalse(current.path.with_name('checked.json').exists())
        self.assertEqual(set(api.registry), {100, 107})
        self.assertEqual(api.registry[100].status, 'completed')
        api.monitor.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in protected}, before)

    def test_rerun_preview_accepts_changed_settings_without_resetting_state(self):
        saved, api = self.submitted(self.settings())
        checked = saved.path.with_name('checked.json')
        state.write_json(checked, dict(state_id=saved.data['id'], revision=saved.data['revision'],
                                     successful=[], problems=[]))
        changed = self.settings(first_event=1)
        before = {path: path.read_bytes() for path in (saved.path, checked)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['rerun', changed['config_file']], api), 0)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(api.registry), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['rerun', changed['config_file'], '--submit'], api), 0)
        current = state.State(changed)
        self.assertEqual(current.data['groups'][0]['units'][0]['spec']['first'], 1)
        self.assertFalse(checked.exists())

    def test_rerun_planning_failure_preserves_state_and_checker_export(self):
        record = self.record()
        upstream = self.exported('disgen', [record])
        saved, api = self.submitted(self.settings('disreplay_gen'))
        checked = saved.path.with_name('checked.json')
        state.write_json(checked, dict(state_id=saved.data['id'], revision=saved.data['revision'],
                                     successful=[], problems=[]))
        upstream.path.with_name('checked.json').unlink()
        before = {path: path.read_bytes() for path in (saved.path, checked)}
        with self.assertRaisesRegex(ValueError, 'Run check'):
            workflow.main(['rerun', saved.settings['config_file'], '--submit'], api)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(api.registry), 1)

    def test_rerun_test_mode_preserves_production_and_requires_ganga_to_submit(self):
        production, _ = self.submitted(self.settings())
        production_before = production.path.read_bytes()
        saved, api = self.submitted(self.settings(test=True))
        before = saved.path.read_bytes()
        args = ['rerun', saved.settings['config_file'], '--test', '--submit']
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            workflow.main(args)
        self.assertEqual(saved.path.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(args, api), 0)
        self.assertNotEqual(state.State(saved.settings).data['id'], saved.data['id'])
        self.assertEqual(production.path.read_bytes(), production_before)

    def test_rerun_option_rejects_merger_and_conflicting_actions(self):
        settings = self.settings('merger')
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            workflow.main([settings['config_file'], '--rerun', '--submit'])
        self.assertIn('--rerun requires a production step YAML', error.getvalue())
        self.assertFalse(Path(settings['step_dir']).exists())
        for action in ('check', 'status', 'retry', 'merge'):
            with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                workflow.main([action, settings['config_file'], '--rerun'])

    def test_workflow_launcher_help_does_not_start_ganga(self):
        result = subprocess.run(['bash', str(ROOT / 'prodWorkflow/gangaSubmit.sh'), 'retry', '--help'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('checker-rejected terminal ranges', result.stdout)

    def test_status_polls_before_snapshot_and_allows_unfinished_jobs(self):
        settings = self.settings()
        saved, api = self.submitted(settings)
        job = api.registry[100]
        job.subjobs = [SimpleNamespace(id=i, status='submitted') for i in range(3)]
        def monitor(jobs):
            self.assertEqual(jobs, [job])
            job.subjobs[0].status = 'completed'
            job.subjobs[1].status = 'failed'
            job.subjobs[2].status = 'running'
        api.monitor = Mock(side_effect=monitor)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(['status', settings['config_file']], api), 0)
        api.monitor.assert_called_once_with([job])
        refreshed = state.State(settings)
        self.assertEqual(list(refreshed.data['attempts'][0]['statuses'].values()),
                         ['completed', 'failed', 'running'])
        self.assertIn("'completed': 1", output.getvalue())

    def test_check_polls_and_saves_before_fairship_handoff(self):
        settings = self.settings(test=True)
        saved, api = self.submitted(settings)
        job = api.registry[100]
        api.monitor = Mock(side_effect=lambda jobs: setattr(job, 'status', 'completed'))
        def fairship(settings, argv):
            refreshed = state.State(settings)
            self.assertEqual(list(refreshed.data['attempts'][0]['statuses'].values()), ['completed'])
            return 0
        with patch.object(workflow, 'fairship', side_effect=fairship):
            self.assertEqual(workflow.main(['check', settings['config_file'], '--test'], api), 0)
        api.monitor.assert_called_once_with([job])

    def test_refresh_does_not_monitor_foreign_or_missing_jobs(self):
        saved, api = self.submitted(self.settings())
        monitor = Mock()
        api.registry[100].comment = 'another workflow'
        saved.refresh(api.jobs, monitor)
        monitor.assert_not_called()
        self.assertIn('identity differs', saved.data['attempts'][0]['error'])
        api.registry.clear()
        saved.refresh(api.jobs, monitor)
        monitor.assert_not_called()
        self.assertEqual(set(saved.data['attempts'][0]['statuses'].values()), {'unknown'})

    def test_backend_monitor_waits_for_split_and_unsplit_jobs(self):
        class Backend:
            @staticmethod
            def updateMonitoringInformation(jobs):
                for job in jobs:
                    job.status = 'completed'
        children = [SimpleNamespace(status=status, backend=Backend())
                    for status in ('running', 'submitted', 'failed')]
        master = SimpleNamespace(subjobs=children, updateMasterJobStatus=Mock())
        single = SimpleNamespace(subjobs=[], status='submitted', backend=Backend())
        proxy = SimpleNamespace(stripProxy=lambda job: job)
        with patch.dict(sys.modules, {'GangaCore.GPIDev.Base.Proxy': proxy}), \
                patch.object(Backend, 'updateMonitoringInformation', wraps=Backend.updateMonitoringInformation) as poll, \
                contextlib.redirect_stdout(io.StringIO()):
            submission.monitor([master, single])
        poll.assert_called_once_with([children[0], children[1], single])
        self.assertEqual([job.status for job in children], ['completed', 'completed', 'failed'])
        self.assertEqual(single.status, 'completed')
        master.updateMasterJobStatus.assert_called_once_with()

    def test_monitor_failure_stops_check_before_fairship(self):
        settings = self.settings(test=True)
        saved, api = self.submitted(settings)
        api.monitor = Mock(side_effect=ValueError('Backend status refresh failed'))
        before = saved.path.read_text()
        with patch.object(workflow, 'fairship') as fairship, \
                self.assertRaisesRegex(ValueError, 'Backend status refresh failed'):
            workflow.main(['check', settings['config_file'], '--test'], api)
        fairship.assert_not_called()
        self.assertEqual(saved.path.read_text(), before)

    def test_repeated_test_submission_explains_existing_job_without_side_effects(self):
        settings = self.settings(test=True)
        saved, api = self.submitted(settings)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main([settings['config_file'], '--test', '--submit'], api), 0)
        self.assertIn('No new eligible work', output.getvalue())
        self.assertIn('recorded Ganga job IDs: 100', output.getvalue())
        self.assertEqual(len(api.registry), 1)
        self.assertEqual(len(state.State(settings).data['attempts']), 1)
        # An empty submission must not even touch Ganga storage configuration.
        submission.submit(saved, [], SimpleNamespace())

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
        for source in (ROOT / 'prodWorkflow/worker.py', ROOT / 'prodWorkflow/wn_script_pixi.py'):
            shutil.copyfile(source, sandbox / source.name)
        spec = self.prepared().data['groups'][0]['units'][0]['spec']
        (sandbox / 'task.json').write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-B', str(sandbox / 'worker.py'), 'task.json', '--prepare-only'],
                                cwd=sandbox, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INFO: Executing:', result.stdout)
        self.assertIn('Exit code: 0', result.stdout)

    def test_workflow_runs_with_only_local_helpers(self):
        import shutil
        isolated = self.base / 'isolated'
        package = isolated / 'prodWorkflow'
        package.mkdir(parents=True)
        for source in (ROOT / 'prodWorkflow').glob('*.py'):
            shutil.copyfile(source, package / source.name)
        code = '''
import importlib
from pathlib import Path
import sys
from types import ModuleType
sys.path.insert(0, sys.argv[1])
helpers = ('checkAnalysis', 'checkFilterOutputs', 'checkPrepareEventsOutputs',
           'checkReplay', 'checkReplayFilter', 'gangaProdScript', 'jobTiming',
           'preparationInputs', 'productionState', 'replayChain', 'replayProduction',
           'replayfilter_worker', 'wn_script_pixi')
# Legacy modules loaded by another application must not shadow local copies.
for name in helpers:
    sys.modules[name] = ModuleType(name)
for name in (*helpers, 'workflow', 'checking', 'planning', 'state', 'worker'):
    module = importlib.import_module('prodWorkflow.' + name)
    assert Path(module.__file__).parent == Path(sys.argv[1]) / 'prodWorkflow'
from prodWorkflow import checking, workflow
assert checking.preparation.__name__ == 'prodWorkflow.checkPrepareEventsOutputs'
try:
    workflow.main(['--help'])
except SystemExit as error:
    assert error.code == 0
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(isolated)],
                                cwd=isolated, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_submission_packages_local_worker_helpers(self):
        saved, api = self.submitted(self.settings())
        job = api.registry[100]
        folder = ROOT / 'prodWorkflow'
        self.assertEqual(job.application.exe, str(folder / 'worker.py'))
        self.assertEqual([Path(path) for path in job.inputfiles if path.endswith('.py')],
                         [folder / name for name in ('wn_script_pixi.py', 'replayfilter_worker.py', 'replayChain.py', 'replayDiagnostics.py')])

    def analysis_archive(self, tree_only=False, entries=3, version=8):
        directory = self.base / 'analysis_evt0_3'
        directory.mkdir()
        manifest = dict(output_version=version, tree_only=tree_only,
                        normalization=dict(preparation_complete=True, records=[dict(id='prepared-id', **NORMALIZATION['records']['prepared-id'])]),
                        analysis_signature=dict(type='disreplay_gen', sha256='analysis-code'),
                        config=dict(save_plots=False, tree_only=tree_only), entries_processed=entries, entries_available=entries,
                        chain_entry_start=0, chain_entry_stop=entries, weight_definition='test',
                        files=['/input.root'], source_ranges=[dict(path='/input.root', file_uuid='file-id', tree='cbmsim', start=0, stop=entries, entries=entries)] if entries else [])
        (directory / 'run.json').write_text(json.dumps(manifest))
        names = ['normalization.csv', 'events.root']
        if not tree_only:
            names += ['histograms.npz', 'histogram_flows.csv', 'cutflow.csv',
                      'pair_cutflow.csv', 'pair_composition_cutflow.csv', 'tagger_cutflow.csv']
        for name in names:
            (directory / name).write_text('content\n')
        archive = Path(str(directory) + '.tar.gz')
        worker.archive_analysis(directory)
        analysis = checking.checkAnalysis.read_analysis_archive(str(archive))
        analysis['archive'] = str(analysis['archive'])
        return dict(id='analysis-unit', sources=[str(archive)], files=[state.fingerprint(archive)], entries=entries, analysis=analysis)

    def test_merge_skips_extracting_events_and_records_exact_archives(self):
        record = self.analysis_archive()
        archive = record['sources'][0]
        self.exported('analysis', [record])
        install = self.base / 'FairShip/newMuonDIS'
        install.mkdir(parents=True)
        (install / 'mergeAnalysis.py').write_text('# test stub; subprocess mocked\n')
        settings = self.settings('merger', fs_install=str(install.parent), merge_output=str(self.base / 'merged'),
                                 skip_event_records=True)
        saved = state.State(settings)
        with patch.object(checking.checkAnalysis.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.merge(saved, execute=True), 0)
        extracted = self.base / 'merged_inputs/0/analysis_evt0_3'
        self.assertFalse((extracted / 'events.root').exists())
        self.assertTrue((extracted / 'cutflow.csv').is_file())
        self.assertEqual(json.loads((extracted / 'run.json').read_text())['omitted_files'], ['events.root'])
        self.assertIn('--skip-event-records', run.call_args.args[0])
        self.assertEqual(json.loads(saved.path.read_text())['merge']['inputs'], [str(archive)])

    def test_replayfilter_merge_preview_and_execution_are_wired_to_merger(self):
        from prodWorkflow import replayMerge
        record = self.analysis_archive(tree_only=True)
        self.exported('analysis', [record])
        saved = state.State(self.settings('merger', merge_replayfilter=True, summarize=False))
        plan = dict(inputs=[dict(path='/filtered.root')], entries=3,
                    destination=str(self.base / 'eos-common/merged'))
        output = io.StringIO()
        with patch.object(replayMerge, 'plan_merge', return_value=plan) as make_plan, \
                patch.object(replayMerge, 'execute') as merge_filter, \
                patch.object(checking.checkAnalysis, 'merge_analysis') as merge_analysis, \
                contextlib.redirect_stdout(output):
            workflow.merge(saved)
            make_plan.assert_called_once_with([record['analysis']], test=False, defer_diagnostics=False)
            merge_filter.assert_not_called()
            merge_analysis.assert_not_called()
            self.assertIn(plan['destination'], output.getvalue())
            self.assertIn('whole replayfilter files', output.getvalue())
            self.assertFalse(saved.path.exists())
            workflow.merge(saved, execute=True)
            local = self.base / 'results/TRY2026/unit_test/production/merger/replayfilter_merged'
            merge_filter.assert_called_once_with(saved, plan, local)
            merge_analysis.assert_called_once()
        self.assertEqual(saved.data['merge']['inputs'], record['sources'])

    def test_failed_replayfilter_transfer_precedes_analysis_merge(self):
        from prodWorkflow import replayMerge
        self.exported('analysis', [self.analysis_archive(tree_only=True)])
        saved = state.State(self.settings('merger', merge_replayfilter=True, summarize=False))
        plan = dict(inputs=[dict(path='/filtered.root')], entries=3,
                    destination=str(self.base / 'eos-common/merged'))
        with patch.object(replayMerge, 'plan_merge', return_value=plan), \
                patch.object(replayMerge, 'execute', side_effect=OSError('transfer failed')), \
                patch.object(checking.checkAnalysis, 'merge_analysis') as merge_analysis, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(OSError, 'transfer failed'):
            workflow.merge(saved, execute=True)
        merge_analysis.assert_not_called()
        self.assertNotIn('merge', saved.data)

    def test_deferred_diagnostics_uses_saved_plan_without_analysis_or_upstream(self):
        from prodWorkflow import replayMerge
        saved = state.State(self.settings('merger', merge_replayfilter=True, defer_diagnostics=True))
        with self.assertRaisesRegex(ValueError, 'Complete the baseline'):
            workflow.merge_diagnostics(saved)
        plan = dict(id='baseline', diagnostics=True, defer_diagnostics=True, inputs=[],
                    destination=str(self.base / 'published/merged'))
        saved.data['replayfilter_merge'] = dict(plan=plan, phase='published', local=str(self.base / 'local/baseline'))
        saved.save()
        with patch.object(workflow, 'upstream', side_effect=AssertionError('Must use saved plan')), \
                patch.object(replayMerge, 'published_manifest') as validate, \
                patch.object(replayMerge, 'execute') as execute, \
                patch.object(checking.checkAnalysis, 'merge_analysis') as analysis, \
                contextlib.redirect_stdout(io.StringIO()):
            workflow.main(['merge-diagnostics', saved.settings['config_file']])
            execute.assert_not_called()
            validate.assert_not_called()
            workflow.main(['merge-diagnostics', saved.settings['config_file'], '--submit'])
            validate.assert_called_once_with(plan['destination'], plan)
            self.assertEqual(execute.call_args.args[1], replayMerge.plan_diagnostics(plan))
            self.assertEqual(execute.call_args.args[2], self.base / 'local/replayfilter_diagnostics')
            analysis.assert_not_called()

    def test_split_diagnostics_publish_in_order_and_release_each_part(self):
        from prodWorkflow import replayMerge
        saved = state.State(self.settings('merger', merge_replayfilter=True, defer_diagnostics=True))
        inputs = [dict(path=f'/input/{i}.root', files=[dict(path=f'/input/{i}.root', size=3 * 1024 ** 3)],
                       entry_start=i * 2, entry_stop=(i + 1) * 2) for i in range(3)]
        plan = dict(id='baseline', diagnostics=True, defer_diagnostics=True, inputs=inputs,
                    destination=str(self.base / 'published/merged'))
        saved.data['replayfilter_merge'] = dict(plan=plan, phase='published', local=str(self.base / 'local/baseline'))
        saved.save()
        command = ['merge-diagnostics', saved.settings['config_file'], '--split']
        with patch.object(workflow, 'upstream', side_effect=AssertionError('Must use saved plan')), \
                patch.object(replayMerge, 'published_manifest'), patch.object(replayMerge, 'execute') as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            workflow.main(command)
            execute.assert_not_called()
            workflow.main([*command, '--submit'])
            self.assertEqual(len(execute.call_args_list), 4)
            self.assertEqual([call.args[1] for call in execute.call_args_list],
                             replayMerge.plan_diagnostic_parts(plan))
            self.assertTrue(all(call.kwargs['release_local'] for call in execute.call_args_list))
            self.assertEqual(state.State(saved.settings).data['replayfilter_diagnostics_parts']['phase'], 'published')
            with self.assertRaisesRegex(ValueError, 'keep the original'):
                workflow.main([*command, '--tree-part-gib', '8', '--submit'])

    def test_tree_only_archive_accepts_zero_events_without_histograms(self):
        record = self.analysis_archive(tree_only=True, entries=0)
        checking.checkAnalysis.validate_analysis_sources([record['analysis']])
        self.assertTrue(record['analysis']['manifest']['tree_only'])
        self.assertFalse((self.base / 'analysis_evt0_3/histograms.npz').exists())

    def test_version_seven_archive_remains_readable(self):
        record = self.analysis_archive(tree_only=True, version=7)
        checking.checkAnalysis.validate_analysis_sources([record['analysis']])
        self.assertEqual(record['analysis']['manifest']['output_version'], 7)

    def test_unsupported_analysis_versions_rejected_by_worker_and_checker(self):
        record = self.analysis_archive(tree_only=True)
        directory = self.base / 'analysis_evt0_3'
        manifest = record['analysis']['manifest']
        for version in (None, 2, 9):
            with self.subTest(version=version):
                manifest['output_version'] = version
                (directory / 'run.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'Unsupported analysis output_version'):
                    worker.archive_analysis(directory)
                with tarfile.open(record['sources'][0], 'w:gz') as archive:
                    archive.add(directory, arcname=directory.name)
                with self.assertRaisesRegex(ValueError, 'Unsupported analysis output_version'):
                    checking.checkAnalysis.read_analysis_archive(record['sources'][0])

    def test_mixed_analysis_versions_rejected(self):
        result = self.analysis_archive(tree_only=True)['analysis']
        other = deepcopy(result)
        other['manifest']['output_version'] = 7
        with self.assertRaisesRegex(ValueError, 'Incompatible analysis settings/code'):
            checking.checkAnalysis.validate_analysis_sources([result, other])

    def test_version_eight_root_check_validates_schema_and_entry_count(self):
        result = self.analysis_archive(tree_only=True)['analysis']
        file = Mock()
        file.IsZombie.return_value = file.TestBit.return_value = False
        file.Get.return_value.GetEntries.return_value = 3
        root = SimpleNamespace(TFile=SimpleNamespace(Open=Mock(return_value=file), kRecovered=2))
        module = SimpleNamespace(TREE_NAME='DIS', validate_tree=Mock())
        checker = checking.checkAnalysis
        with patch.dict(sys.modules, ROOT=root), \
                patch.object(checker.importlib.util, 'spec_from_file_location', return_value=Mock()), \
                patch.object(checker.importlib.util, 'module_from_spec', return_value=module), \
                patch.object(checker, 'path_statistics', return_value={'paths': 'checked'}) as statistics:
            self.assertEqual(checker.check_event_tree(result, self.base), {'paths': 'checked'})
            module.validate_tree.assert_called_once()
            self.assertIs(module.validate_tree.call_args.args[0], file.Get.return_value)
            statistics.assert_called_once_with(file.Get.return_value, analysis=True, scan=True)
            file.Close.assert_called_once()
            file.Get.return_value.GetEntries.return_value = 2
            with self.assertRaisesRegex(ValueError, 'entries differ'):
                checker.check_event_tree(result, self.base)
            self.assertEqual(file.Close.call_count, 2)
            self.assertEqual(statistics.call_count, 1)

    def test_version_eight_requires_nonempty_root_even_for_zero_events(self):
        record = self.analysis_archive(tree_only=True, entries=0)
        directory = self.base / 'analysis_evt0_3'
        (directory / 'events.root').write_bytes(b'')
        worker.archive_analysis(directory)
        with self.assertRaisesRegex(ValueError, 'empty events.root'):
            checking.checkAnalysis.read_analysis_archive(record['sources'][0])
        (directory / 'events.root').unlink()
        (directory / 'events.jsonl').write_text('old event records')
        worker.archive_analysis(directory)
        with self.assertRaisesRegex(KeyError, 'events.root'):
            checking.checkAnalysis.read_analysis_archive(record['sources'][0])

    def test_mixed_tree_only_and_combined_outputs_rejected(self):
        result = self.analysis_archive(tree_only=True)['analysis']
        combined = deepcopy(result)
        combined['manifest']['tree_only'] = False
        with self.assertRaisesRegex(ValueError, 'mix tree-only'):
            checking.checkAnalysis.validate_analysis_sources([result, combined])

    def test_mixed_analysis_diagnostic_schemas_rejected(self):
        result = self.analysis_archive(tree_only=True)['analysis']
        other = deepcopy(result)
        result['manifest']['event_record_schema'] = dict(stages=['entrance clearance >= 20 cm'])
        other['manifest']['event_record_schema'] = dict(stages=['old clearance'])
        with self.assertRaisesRegex(ValueError, 'diagnostics/schema'):
            checking.checkAnalysis.validate_analysis_sources([result, other])

    def tree_merge(self, **settings):
        record = self.analysis_archive(tree_only=True)
        self.exported('analysis', [record], test=settings.get('test', False))
        install = self.base / 'FairShip/newMuonDIS'
        install.mkdir(parents=True)
        for script in ('mergeAnalysis.py', 'summarizeAnalysis.py'):
            (install / script).write_text('# test stub; subprocess mocked\n')
        settings.setdefault('merge_output', str(self.base / 'merged'))
        return state.State(self.settings('merger', fs_install=str(install.parent), **settings))

    def assert_default_merge_locations(self, test=False):
        saved = self.tree_merge(merge_output=None, test=test, name='custom_merger')
        settings = saved.settings
        mode = 'test' if test else 'production'
        local = self.base / 'results' / settings['tag'] / settings['production'] / mode / settings['name']
        for write_to_eos, parent in ((False, local), (True, Path(settings['output_dir']))):
            with self.subTest(write_to_eos=write_to_eos):
                # Exercise both the YAML shorthand and the explicit merge command.
                argv = [settings['config_file'], '--submit']
                if write_to_eos:
                    argv = ['merge', *argv, '--writeToEOS']
                if test:
                    argv.append('--test')
                with patch.object(workflow.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(workflow.main(argv), 0)
                self.assertEqual(run.call_count, 2)
                merge_command, summary_command = [call.args[0] for call in run.call_args_list]
                self.assertEqual(merge_command[-3:], ['-o', str(parent / 'merged'), '--no-plots'])
                self.assertEqual(summary_command[2:], [str(parent / 'merged'), '-o', str(parent / 'merged_summary'), '--no-plots'])
                self.assertTrue((parent / 'merged_inputs/0/analysis_evt0_3/events.root').is_file())
                recorded = state.State(settings).data['merge']
                self.assertEqual(recorded['output'], str(parent / 'merged'))
                self.assertEqual(recorded['summary'], str(parent / 'merged_summary'))

    def test_merge_defaults_to_local_with_eos_opt_in(self):
        self.assert_default_merge_locations()

    def test_merge_test_mode_has_separate_local_and_eos_folders(self):
        self.assert_default_merge_locations(test=True)

    def test_merge_explicit_paths_override_eos_flag_and_keep_test_suffix(self):
        saved = self.tree_merge(test=True, unpack_dir=str(self.base / 'retained'), save_plots=True)
        with patch.object(workflow.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            workflow.merge(saved, execute=True, write_to_eos=True)
        self.assertEqual(run.call_args_list[0].args[0][-2:], ['-o', str(self.base / 'merged_test')])
        self.assertEqual(run.call_args_list[1].args[0][2:],
                         [str(self.base / 'merged_test'), '-o', str(self.base / 'merged_test_summary')])
        self.assertTrue((self.base / 'retained_test/0/analysis_evt0_3/events.root').is_file())

    def test_merge_eos_flag_survives_ganga_handoff(self):
        saved = self.tree_merge(merge_output=None)
        argv = [saved.settings['config_file'], '--writeToEOS', '--submit']
        with patch.object(workflow, 'fairship', return_value=0) as fairship:
            self.assertEqual(workflow.main(argv, self.api()), 0)
        self.assertEqual(fairship.call_args.args[1], ['plan', *argv])

    def test_eos_flag_rejected_for_non_merger_step(self):
        settings = self.settings()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            workflow.main([settings['config_file'], '--writeToEOS'])
        self.assertEqual(error.exception.code, 2)

    def test_local_merge_preview_does_not_create_results(self):
        saved = self.tree_merge(merge_output=None)
        with patch.object(workflow.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['merge', saved.settings['config_file']]), 0)
        run.assert_not_called()
        self.assertFalse((self.base / 'results').exists())

    def test_tree_merge_then_summary_retains_root_and_records_both_outputs(self):
        saved = self.tree_merge(save_plots=False)
        with patch.object(workflow.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.merge(saved, execute=True), 0)
        self.assertEqual(run.call_count, 2)
        merge_command, summary_command = [call.args[0] for call in run.call_args_list]
        self.assertTrue(merge_command[1].endswith('/mergeAnalysis.py'))
        self.assertNotIn('--skip-event-records', merge_command)
        self.assertTrue(summary_command[1].endswith('/summarizeAnalysis.py'))
        self.assertEqual(summary_command[2:], [str(self.base / 'merged'), '-o',
                                             str(self.base / 'merged_summary'), '--no-plots'])
        self.assertTrue((self.base / 'merged_inputs/0/analysis_evt0_3/events.root').is_file())
        self.assertEqual(saved.data['merge']['summary'], str(self.base / 'merged_summary'))

    def test_tree_merge_can_defer_summary(self):
        saved = self.tree_merge(summarize=False)
        with patch.object(workflow.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            workflow.merge(saved, execute=True)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('summary', saved.data['merge'])

    def test_tree_merge_rejects_omission_before_extracting(self):
        saved = self.tree_merge(skip_event_records=True)
        with patch.object(workflow.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Cannot omit event records'):
                workflow.merge(saved, execute=True)
        run.assert_not_called()
        self.assertFalse((self.base / 'merged_inputs').exists())
        args = SimpleNamespace(skip_event_records=True)
        results = [record['analysis'] for record in planning.upstream(saved.settings)['records']]
        with self.assertRaisesRegex(ValueError, 'Cannot omit event records'):
            checking.checkAnalysis.merge_analysis(results, args)

    def test_summary_failure_keeps_successful_merge_record(self):
        saved = self.tree_merge()
        with patch.object(workflow.subprocess, 'run', side_effect=[None, subprocess.CalledProcessError(1, 'summary')]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                workflow.merge(saved, execute=True)
        merged = json.loads(saved.path.read_text())['merge']
        self.assertEqual(merged['output'], str(self.base / 'merged'))
        self.assertNotIn('summary', merged)

    def test_old_analysis_version_rejected_before_merging(self):
        saved = self.tree_merge()
        upstream = Path(saved.settings['campaign_dir']) / 'analysis/checked.json'
        data = json.loads(upstream.read_text())
        data['records'][0]['analysis']['manifest'].update(output_version=6, tree_only=False)
        state.write_json(upstream, data)
        with self.assertRaisesRegex(ValueError, 'requires version-7'):
            workflow.merge(saved, execute=True)

    def test_new_merge_defaults_preserve_upstream_saved_settings(self):
        for step in ('disgen', 'disfilter', 'disreplay_gen', 'disreplay_filter', 'replayfilter'):
            with self.subTest(step=step):
                settings = self.settings(step)
                saved = state.State(settings)
                original = config.signature(settings)
                original.pop('summarize', None)
                original['skip_event_records'] = True
                saved.data['configuration'] = original
                saved.save()
                self.assertEqual(state.State(settings).data['id'], saved.data['id'])

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
        self.check_replayfilter_empty_output(entries=10, limit=-1, processed=10)

    def test_replayfilter_check_accepts_limited_processing(self):
        self.check_replayfilter_empty_output(entries=25000, limit=10000, processed=10000)

    def test_replayfilter_check_validates_analytical_mode(self):
        self.check_replayfilter_empty_output(entries=10, limit=-1, processed=10, cross_section='analytical')

    def check_replayfilter_empty_output(self, entries, limit, processed, cross_section=None):
        record = self.record(entries=entries, name='sim_a_disreplay_gen.root')
        geometry = self.base / 'geo_a_disreplay_gen.root'
        geometry.write_bytes(b'geometry')
        record.update(master=str(self.base / 'prepared.root'), replay_type='disreplay_gen', geometries=[str(geometry)])
        record['files'].append(state.fingerprint(geometry))
        self.exported('disreplay_gen', [record])
        options = {} if cross_section is None else dict(cross_section=cross_section)
        saved, _ = self.submitted(self.settings('replayfilter', events_per_file=limit, **options))
        unit = saved.data['groups'][0]['units'][0]
        output, geo, manifest_path = map(Path, saved.data['attempts'][0]['outputs'][unit['id']])
        output.parent.mkdir(parents=True)
        output.write_bytes(b'ROOT with zero selected entries')
        geo.write_bytes(b'geometry')
        counts = dict(source_entry_start=0, source_entry_stop=processed, entries_available=entries,
                      events_processed=processed, events_two_tracks=0, events_selected=0)
        if cross_section is not None:
            counts['cross_section'] = cross_section
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
        if cross_section is not None:
            payload['filter']['cross_section'] = 'stored'
            manifest_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'cross section differs'):
                checking.checkReplayFilter.read_filter_manifest(manifest_path)


if __name__ == '__main__':
    unittest.main()
