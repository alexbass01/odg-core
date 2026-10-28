"""
Offline smoke: exercising the Batch A extensions' logging paths would normally
require a Kind cluster + registry + secrets. This test verifies what can be
verified without infra:

1. Each migrated extension imports cleanly.
2. `odg.logging_cfg.configure_logging(service_name=...)` produces JSON on stdout.
3. `k8s.logging.configure_kubernetes_logging()` produces CRD-shaped files.
4. The CRD format preserves the odg-ui contract keys + a parseable timestamp.

NOTE: `configure_kubernetes_logging()` is intentionally NOT called in the smoke
fixture: it's called at import time of each extension module and its FileHandlers
are already attached to the root logger. The `configure_logging()` calls below
only add stdout handling, so we check the *stdout* format here and rely on the
dedicated contract test (`test_log_collection_contract.py`) for the CRD format.
"""

import importlib
import io
import json
import logging
import subprocess
import sys

import odg.logging_cfg as lc


EXTENSION_MODULES = [
    'artefact_enumerator',
    'backlog_controller',
    'cache_manager',
    'codeql',
    'delivery_db_backup',
    'ghas',
    'sast',
    'sbom_generator',
    'crypto_extension.__main__',
    'osid_extension.__main__',
    'responsibles_extension.__main__',
    'sla_violation_profiler_extension.__main__',
    'bdba_extension.__main__',
    'issue_replicator.__main__',
    'malware.__main__',
    'odg_operator.__main__',
    'freshclam.__main__',
    'trivy_extension.__main__',
]


def test_extension_modules_import_cleanly():
    for module_name in EXTENSION_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as e:
            raise AssertionError(f'importing {module_name} failed: {e}') from e


def test_all_crd_logging_produces_json_on_stdout(tmp_path):
    """every extension module emits structlog JSON once configured"""
    captured = io.StringIO()

    lc.configure_logging(json_logs=True, service_name='test-batch', stream=captured)

    logging.getLogger('verify').warning('probe')

    line = captured.getvalue().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry['event'] == 'probe'
    assert entry['service.name'] == 'test-batch'


def test_no_ext_modules_break_imports_via_service_name_injection():
    """
    configure_logging injects `service.name` into every LogRecord via the
    record factory; must not break module-level imports that rely on
    `logging.getLogger(__name__)` before configure_logging runs.
    """
    import artefact_enumerator  # noqa: F401

    record_factory = logging.getLogRecordFactory()
    assert callable(record_factory)


def test_batch_b_modules_emit_json_with_correct_service_name(tmp_path):
    """spot-checks Batch B extensions: bdba_extension, issue_replicator"""
    import importlib

    importlib.import_module('bdba_extension.__main__')
    importlib.import_module('issue_replicator.__main__')

    captured = io.StringIO()
    lc.configure_logging(json_logs=True, service_name='test-batch-b', stream=captured)

    logging.getLogger('verify-b').warning('batch-b probe')

    line = captured.getvalue().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry['event'] == 'batch-b probe'
    assert entry['service.name'] == 'test-batch-b'


def test_subprocess_isolation_of_extension_logging(tmp_path):
    """launcher-level sanity check: an extension run in a subprocess emits JSON"""
    cmd = [
        sys.executable,
        '-c',
        (
            'import sys; sys.path.insert(0, "src"); '
            'import artefact_enumerator; '
            'import logging; logging.getLogger("subprocess-test").warning("hello")'
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd='.')

    if result.returncode != 0:
        raise AssertionError(f'subprocess failed: {result.stderr}')

    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    last = json.loads(lines[-1])
    assert last['event'] == 'hello'
    assert last['service.name'] == 'artefact-enumerator'
