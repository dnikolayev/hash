#!/usr/bin/env python3
"""Print a fork-only smoke or complete-Test benchmark workflow.

Both modes check out the requested source commit. Baseline mode retains the
original Cargo compile command; only the candidate prepares and reuses the build
output. Compare identical application source and dependencies, disclosing any
optimization-only revision differences. Push each generated workflow to its own
benchmark branch.
Repeat a successful cold run with an empty commit on that SAME branch for a
fresh-runner warm measurement. Keep the workflow bytes and source SHA fixed.
"""
import argparse
from pathlib import Path
import re
import subprocess

CACHE_ACTION = '0057852bfaa89a56745cba8c7296529d2fc39830'
SOURCE_MINIO_CLIENT_IMAGE = ('minio/mc:RELEASE.2025-08-13T08-35-41Z@'
                             'sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727')
BENCHMARK_MINIO_CLIENT_IMAGE = ('quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@'
                                'sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727')


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'Expected one workflow anchor: {old.splitlines()[0]}')
    return text.replace(old, new, 1)


def pin_checkouts(text, source_sha):
    lines = text.splitlines(keepends=True)
    result = []
    for index, line in enumerate(lines):
        result.append(line)
        if 'uses: actions/checkout@' in line:
            if index + 1 < len(lines) and lines[index + 1].strip() == 'with:':
                lines[index + 1] += f'          ref: {source_sha}\n'
            else:
                result.extend(['        with:\n', f'          ref: {source_sha}\n'])
    return ''.join(result)


def generate(original, baseline_sha, candidate_sha, variant, campaign, sample, scope):
    header, jobs = original.split('jobs:\n', 1)
    begin = jobs.index('  integration-tests:\n')
    end = jobs.index('  publish-rust:\n', begin)
    integration = jobs[begin:end]
    original_steps = integration[integration.index('    steps:\n'):]
    graph_mode = '1' if variant == 'candidate' else '0'
    integration = replace_once(integration, '    name: Integration\n', f'    name: Integration\n    env:\n      HASH_GRAPH_BUILD_CACHE: "{graph_mode}"\n')
    if scope == 'smoke':
        integration = replace_once(integration, '    needs: [setup, sccache-credentials]\n', '')
        integration = replace_once(integration, '      matrix: ${{ fromJSON(needs.setup.outputs.integration-tests) }}\n', '''      matrix:
        include:
          - name: "@tests/hash-playwright"
            path: tests/hash-playwright
          - name: "@tests/hash-backend-integration"
            path: tests/hash-backend-integration
''')
        first = integration.index('    # Only the top of a stack')
        last = integration.index('    runs-on:', first)
        integration = integration[:first] + integration[last:]
    namespace = f'graph-build-benchmark-{campaign}-{sample}'
    key = namespace + '-${{ steps.graph-build.outputs.key }}'
    prepare = f'''      - name: Prepare graph build
        id: graph-build
        if: env.HASH_GRAPH_BUILD_CACHE == '1' && hashFiles('apps/hash-graph/package.json') != ''
        run: python3 .github/actions/graph-build-cache/graph_build_cache.py prepare

      - name: Require graph build preparation
        if: env.HASH_GRAPH_BUILD_CACHE == '1' && hashFiles('apps/hash-graph/package.json') != ''
        env:
          PREPARED_KEY: ${{{{ steps.graph-build.outputs.key }}}}
        run: test -n "$PREPARED_KEY"

      - name: Restore graph build
        id: graph-cache
        if: env.HASH_GRAPH_BUILD_CACHE == '1' && steps.graph-build.outputs.key != ''
        uses: actions/cache/restore@{CACHE_ACTION} # v4.3.0
        with:
          path: target/graph-build-cache
          key: {key}

'''
    registry = f'''      - name: Use available MinIO client registry
        run: |
          python3 - <<'PYTHON'
          import os, pathlib
          path = pathlib.Path('infra/compose/compose.yml')
          old = b'    image: {SOURCE_MINIO_CLIENT_IMAGE}\\n'
          new = ('    image: ' + os.environ['BENCHMARK_MINIO_CLIENT_IMAGE'] + '\\n').encode()
          data = path.read_bytes()
          if data.count(old) != 1:
              raise SystemExit('expected exactly one pinned MinIO client image')
          path.write_bytes(data.replace(old, new))
          print(os.environ['BENCHMARK_MINIO_CLIENT_IMAGE'])
          PYTHON

'''
    record = '''      - name: Record benchmark environment
        run: |
          mkdir -p var/logs
          python3 - <<'PYTHON'
          import json, os, pathlib, platform, subprocess
          def output(command):
              return subprocess.check_output(command, text=True).strip()
          record = {
              'baseline_sha': os.environ['BENCHMARK_BASELINE_SHA'],
              'candidate_sha': os.environ['BENCHMARK_SOURCE_SHA'],
              'checkout_sha': output(['git', 'rev-parse', 'HEAD']),
              'variant': os.environ['BENCHMARK_VARIANT'],
              'sample': os.environ['BENCHMARK_SAMPLE'],
              'scope': os.environ['BENCHMARK_SCOPE'],
              'workflow_sha': os.environ['BENCHMARK_WORKFLOW_SHA'],
              'run_id': os.environ['GITHUB_RUN_ID'],
              'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
              'runner_arch': os.environ['RUNNER_ARCH'],
              'runner_os': os.environ['RUNNER_OS'],
              'runner_name': os.environ['RUNNER_NAME'],
              'image_version': os.environ.get('ImageVersion'),
              'platform': platform.platform(),
              'cpu_count': os.cpu_count(),
              'cpu_lines': sorted({line for line in pathlib.Path('/proc/cpuinfo').read_text().splitlines()
                                   if line.split(':', 1)[0].strip() in
                                   {'model name', 'flags', 'Features', 'CPU implementer', 'CPU part'}}),
              'rustc': output(['rustc', '-vV']),
              'cargo': output(['cargo', '--version']),
              'node': output(['node', '--version']),
              'turbo': output(['turbo', '--version']),
              'turbo_cache': os.environ['TURBO_CACHE'],
              'cargo_incremental': os.environ['CARGO_INCREMENTAL'],
              'sccache': 'disabled',
              'minio_client_image': os.environ['BENCHMARK_MINIO_CLIENT_IMAGE'],
          }
          text = json.dumps(record, indent=2)
          pathlib.Path('var/logs/benchmark-environment.json').write_text(text + '\\n')
          print(text)
          PYTHON

'''
    anchor = '      - name: Find test steps to run\n'
    integration = replace_once(integration, anchor, registry + record + prepare + anchor)
    validate_if = "env.HASH_GRAPH_BUILD_CACHE == '1' && steps.graph-build.outputs.key != ''"
    save_if = ("env.HASH_GRAPH_BUILD_CACHE == '1' && steps.graph-cache.outputs.cache-hit != 'true' "
               "&& steps.graph-upload.outputs.valid == 'true' "
               "&& steps.graph-cache-before-save.outputs.cache-hit != 'true'")
    recheck = f'''      - name: Recheck graph build cache
        id: graph-cache-before-save
        if: env.HASH_GRAPH_BUILD_CACHE == '1' && steps.graph-cache.outputs.cache-hit != 'true' && steps.graph-upload.outputs.valid == 'true'
        continue-on-error: true
        uses: actions/cache/restore@{CACHE_ACTION} # v4.3.0
        with:
          path: target/graph-build-cache
          key: {key}
          lookup-only: true

'''
    if variant == 'baseline':
        # Cache mode 0 skips these steps.
        validate_if += " && steps.graph-cache.outputs.cache-hit != 'true'"
        save_if = "steps.graph-upload.outputs.valid == 'true'"
        recheck = ''
    save = f'''      - name: Validate graph build for upload
        id: graph-upload
        if: {validate_if}
        env:
          EXPECTED_KEY: ${{{{ steps.graph-build.outputs.key }}}}
        run: |
          python3 - <<'PYTHON'
          import os, pathlib, runpy
          helper = runpy.run_path('.github/actions/graph-build-cache/graph_build_cache.py')
          with helper['build_lock'](pathlib.Path.cwd()):
              valid = helper['verified_payload_digest'](pathlib.Path('target/graph-build-cache'), os.environ['EXPECTED_KEY']) is not None
          with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
              output.write(f'valid={{str(valid).lower()}}\\n')
          print(f'Graph payload matches prepared key: {{valid}}')
          if not valid:
              print("Graph cache upload skipped: payload is missing, invalid, or bound to different inputs")
          PYTHON

{recheck}      - name: Save graph build
        if: {save_if}
        uses: actions/cache/save@{CACHE_ACTION} # v4.3.0
        with:
          path: target/graph-build-cache
          key: {key}

'''
    integration = replace_once(integration, '      - name: Run tests\n', save + '      - name: Run tests\n')
    recheck_receipt = ('${{ steps.graph-cache-before-save.outputs.cache-hit }}'
                       if variant == 'candidate' else '""')
    receipt = f'''      - name: Record graph cache evidence
        if: always()
        env:
          GRAPH_PREPARED_KEY: ${{{{ steps.graph-build.outputs.key }}}}
          GRAPH_CACHE_HIT: ${{{{ steps.graph-cache.outputs.cache-hit }}}}
          GRAPH_RECHECK_HIT: {recheck_receipt}
          GRAPH_UPLOAD_VALID: ${{{{ steps.graph-upload.outputs.valid }}}}
        run: |
          python3 - <<'PYTHON'
          import json, os, pathlib, shutil
          logs = pathlib.Path('var/logs')
          logs.mkdir(parents=True, exist_ok=True)
          for source in ['target/graph-build-inputs.json', 'target/graph-build-cache/manifest.json']:
              path = pathlib.Path(source)
              if path.is_file() and not path.is_symlink():
                  shutil.copyfile(path, logs / ('graph-' + path.name))
          receipt = {{key: os.environ.get(key, '') for key in ['GRAPH_PREPARED_KEY', 'GRAPH_CACHE_HIT', 'GRAPH_RECHECK_HIT', 'GRAPH_UPLOAD_VALID']}}
          binary = pathlib.Path('target/debug/hash-graph')
          receipt['graph_binary_size_bytes'] = binary.stat().st_size if binary.is_file() and not binary.is_symlink() else None
          (logs / 'graph-cache-receipt.json').write_text(json.dumps(receipt, indent=2) + '\\n')
          print(json.dumps(receipt))
          PYTHON

'''
    integration = replace_once(integration, '      - name: Upload logs\n', receipt + '      - name: Upload logs\n')
    # The original executable startup, healthcheck, service retry and test bodies
    # must remain literal substrings; only the surrounding harness is changed.
    for name in ['Launch external services', 'Start background tasks', 'Run tests']:
        block = original_steps.split(f'      - name: {name}\n', 1)[1].split('\n      - ', 1)[0]
        assert block in integration, name
    # Native summaries time current task execution, including wrapper overhead.
    # Apply the same instrumentation after verifying the original command bodies.
    integration = replace_once(integration, '          turbo run compile --env-mode=loose\n',
                               '          turbo run compile --env-mode=loose --summarize\n')
    integration = replace_once(integration,
        '          turbo run test:integration --env-mode=loose --filter "${{ matrix.name }}"\n',
        '          turbo run test:integration --env-mode=loose --filter "${{ matrix.name }}" --summarize\n')
    task_timings = '''      - name: Record current task timings
        if: always()
        run: |
          python3 - <<'PYTHON'
          import json, pathlib
          records = []
          fields = ['startTime', 'endTime', 'exitCode']
          for path in sorted(pathlib.Path('.turbo/runs').glob('*.json')):
              summary = json.loads(path.read_text())
              assert summary['version'] == '1' and summary['turboVersion'] == '2.10.12'
              execution = summary.get('execution') or {}
              record = {key: summary[key] for key in ['id', 'version', 'turboVersion']}
              record['execution'] = {key: execution.get(key) for key in ['command', *fields]}
              record['tasks'] = []
              for task in summary['tasks']:
                  timing = task.get('execution') or {}
                  record['tasks'].append({
                      'taskId': task['taskId'], 'command': task['command'],
                      'execution': {key: timing.get(key) for key in fields},
                      'turbo_cache_status': task['cache']['status'],
                      'task_cache_enabled': task['resolvedTaskDefinition']['cache'],
                  })
              records.append(record)
          logs = pathlib.Path('var/logs')
          logs.mkdir(parents=True, exist_ok=True)
          (logs / 'turbo-task-timings.json').write_text(json.dumps(records, indent=2) + '\\n')
          print(f'Recorded {len(records)} current Turbo summaries')
          PYTHON

'''
    integration = replace_once(integration, '      - name: Record graph cache evidence\n',
                               task_timings + '      - name: Record graph cache evidence\n')
    jobs = integration if scope == 'smoke' else jobs[:begin] + integration + jobs[end:]
    if scope == 'full' and variant == 'candidate':
        checker = '''  graph-build-cache-check:
    name: Graph build cache checks
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    steps:
      - name: Checkout
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      - name: Check graph build reuse
        run: python3 .github/actions/graph-build-cache/graph_build_cache_test.py
      - name: Check CPU cache-line decoding
        run: |
          CHECK_DIR=$(mktemp -d)
          trap 'rm -rf "$CHECK_DIR"' EXIT
          rustc +stable --edition=2021 --test .github/actions/graph-build-cache/cache_line_probe.rs -o "$CHECK_DIR/check"
          "$CHECK_DIR/check"

'''
        jobs = checker + jobs
        jobs = replace_once(jobs,
            '[setup, sccache-credentials, unit-tests, integration-tests, publish-rust]',
            '[\n        setup,\n        sccache-credentials,\n        unit-tests,\n'
            '        integration-tests,\n        publish-rust,\n        graph-build-cache-check,\n      ]')
        jobs = replace_once(jobs, '      - name: Check setup script\n',
            '''      - name: Check graph build cache checks
        run: test "${{ needs.graph-build-cache-check.result }}" = success
      - name: Check setup script
''')
    # No upstream cache/service secrets are supplied. GITHUB_TOKEN remains the
    # ordinary repository-scoped Actions token required to install public tools.
    jobs = re.sub(r'\$\{\{ secrets\.(?!GITHUB_TOKEN\b)[A-Z0-9_]+ \}\}', '""', jobs)
    jobs = re.sub(r'\$\{\{ vars\.[A-Z0-9_]+ \}\}', '""', jobs)
    jobs = jobs.replace('${{ needs.sccache-credentials.outputs.encrypted-credentials }}', '""')
    # Notifications cannot run for push-triggered samples and are not measured.
    if '      - name: Notify Slack on failure\n' in jobs:
        jobs = jobs.split('      - name: Notify Slack on failure\n', 1)[0]
    # Omit an operational comment from the copied public workflow; executable
    # service setup remains unchanged.
    jobs = '\n'.join(line for line in jobs.split('\n') if 'linear.app/' not in line)
    jobs = pin_checkouts(jobs, candidate_sha)
    return f'''# Fork-only harness derived from .github/workflows/test.yml.
# Smoke measures only two integrations; it does not measure complete Test CI.
# All samples disable remote Turbo and sccache.
# The source's MinIO client digest is pulled from its available official registry.
# Compare identical application inputs; disclose optimization-only source differences.
name: Integration build benchmark ({scope})
run-name: ${{{{ github.ref_name }}}} / attempt ${{{{ github.run_attempt }}}}

on:
  push:
    branches:
      - "speedup/integration-build-benchmark-*"

permissions:
  contents: read

env:
  TURBO_CACHE: "local:rw"
  TURBO_TOKEN: ""
  CARGO_INCREMENTAL: "0"
  NEXTEST_PROFILE: ci
  BENCHMARK_BASELINE_SHA: "{baseline_sha}"
  BENCHMARK_SOURCE_SHA: "{candidate_sha}"
  BENCHMARK_VARIANT: "{variant}"
  BENCHMARK_SAMPLE: "{sample}"
  BENCHMARK_SCOPE: "{'integration-only' if scope == 'smoke' else 'complete-test'}"
  BENCHMARK_MINIO_CLIENT_IMAGE: "{BENCHMARK_MINIO_CLIENT_IMAGE}"
  BENCHMARK_WORKFLOW_SHA: ${{{{ github.workflow_sha }}}}

concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
{jobs}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--candidate-ref', required=True)
    parser.add_argument('--variant', choices=['baseline', 'candidate'], required=True)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--sample', choices=['smoke', '1', '2', '3'], required=True)
    parser.add_argument('--scope', choices=['smoke', 'full'], default='smoke')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[a-z0-9-]+', args.campaign):
        parser.error('campaign must be a lowercase alphanumeric slug')
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(args.checkout), *arguments], text=True).strip()
    baseline_sha = git('rev-parse', '--verify', f'{args.baseline_ref}^{{commit}}')
    candidate_sha = git('rev-parse', '--verify', f'{args.candidate_ref}^{{commit}}')
    original = git('show', f'{baseline_sha}:.github/workflows/test.yml') + '\n'
    workflow = generate(original, baseline_sha, candidate_sha, args.variant, args.campaign, args.sample, args.scope).rstrip() + "\n"
    if args.self_test:
        for scope in ['smoke', 'full']:
            for variant in ['baseline', 'candidate']:
                generated = generate(original, baseline_sha, candidate_sha, variant, args.campaign, args.sample, scope)
                assert generated.count('      - name: Prepare graph build\n') == 1
                assert generated.count('      - name: Restore graph build\n') == 1
                assert generated.count('      - name: Recheck graph build cache\n') == (1 if variant == 'candidate' else 0)
                assert generated.count('      - name: Save graph build\n') == 1
                assert generated.count('      - name: Use available MinIO client registry\n') == 1
                assert generated.count("old = b'    image: " + SOURCE_MINIO_CLIENT_IMAGE) == 1
                assert generated.count(BENCHMARK_MINIO_CLIENT_IMAGE) == 1
                assert generated.count(f'          ref: {candidate_sha}\n') == generated.count('uses: actions/checkout@')
                assert not re.search(r'secrets\.(?!GITHUB_TOKEN\b)', generated)
                assert ('  unit-tests:\n' in generated) == (scope == 'full')
                assert ('  passed:\n' in generated) == (scope == 'full')
                assert ('  graph-build-cache-check:\n' in generated) == (scope == 'full' and variant == 'candidate')
                assert 'integration_build_timings_test.py' not in generated
                assert f'graph-build-benchmark-{args.campaign}-{args.sample}-${{{{ matrix.name }}}}' not in generated
                assert generated == generate(original, baseline_sha, candidate_sha, variant, args.campaign, args.sample, scope)
    if args.output:
        args.output.write_text(workflow)
    else:
        print(workflow, end='')


if __name__ == '__main__':
    main()
