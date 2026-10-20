"""Whole-job timing from Ganga stdout and HTCondor accounting logs."""

from datetime import datetime
import re
import statistics


def parse_job_timing(stdout, condor_log):
    """Keep missing or ambiguous timing separate from zero usage."""
    result = {'real_time': None, 'cpu_time': None}
    starts = re.findall(r'^Job start:\s*([^\n]+)$', stdout, re.MULTILINE)
    ends = re.findall(r'^Job end:\s*([^\n]+)$', stdout, re.MULTILINE)
    if len(starts) == len(ends) == 1:
        try:
            start, end = (datetime.strptime(value.strip(), '%a %d %b %H:%M:%S %Y')
                          for value in (starts[0], ends[0]))
            elapsed = (end - start).total_seconds()
            if elapsed >= 0:
                result['real_time'] = elapsed
        except ValueError:
            pass

    # Use the last terminated run, not cumulative usage from earlier attempts
    # or the submit host's (usually zero) local usage.
    terminated = re.findall(r'^005 [^\n]*Job terminated\.\s*\n(.*?)(?=^\.\.\.$|\Z)',
                            condor_log, re.MULTILINE | re.DOTALL)
    if terminated:
        usage = re.findall(
            r'^\s*Usr (\d+) (\d{2}):(\d{2}):(\d{2}), '
            r'Sys (\d+) (\d{2}):(\d{2}):(\d{2})\s+-\s+Run Remote Usage\s*$',
            terminated[-1], re.MULTILINE)
        if len(usage) == 1:
            values = list(map(int, usage[0]))
            if all(values[offset + 1] < 24 and values[offset + 2] < 60
                   and values[offset + 3] < 60 for offset in (0, 4)):
                result['cpu_time'] = sum(
                    values[offset] * 86400 + values[offset + 1] * 3600
                    + values[offset + 2] * 60 + values[offset + 3]
                    for offset in (0, 4))
    return result


def read_job_timing(logfile, stdout):
    try:
        condor_log = logfile.with_name('condorLog').read_text(errors='replace')
    except OSError:
        condor_log = ''
    return parse_job_timing(stdout, condor_log)


def print_job_timing(timings):
    print('\nWhole-job timing for successful subjobs:')
    print('Real: stdout job start/end; CPU: condorLog terminated-run remote user + system usage.')
    for key, label in (('real_time', 'real'), ('cpu_time', 'CPU')):
        values = [item[key] for item in timings if item[key] is not None]
        average = statistics.mean(values) if values else None
        value = f'{average:.2f} s ({average / 3600:.3f} h)' if values else 'unavailable'
        print(f'Average job {label} time: {value}; timing available for {len(values)}/{len(timings)} subjobs')
    paired = [item for item in timings
              if item['real_time'] is not None and item['cpu_time'] is not None]
    real = sum(item['real_time'] for item in paired)
    cpu = sum(item['cpu_time'] for item in paired)
    ratio = f'{100 * cpu / real:.2f}%' if real else 'undefined'
    print(f'Job CPU / real time (ratio of paired totals): {ratio}; {len(paired)}/{len(timings)} subjobs')
