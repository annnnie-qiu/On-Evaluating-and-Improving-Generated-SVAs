"""Record an actual empty final AQ; never manufacture native mutation counts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'engine'))
from shared_results import Checker, environment, signature
from aq_source_parser import extract_assertions


def empty_result(record, scope='hierarchy'):
    if record['assertions'] or record['final_aq_count'] != 0:
        raise ValueError('EMPTY_AQ requires a verified zero-assertion export')
    path = Path(record['aq_file'])
    source = path.read_text()
    blocks, errors = extract_assertions(source)
    if blocks or errors:
        raise ValueError('EMPTY_AQ source contains assertions or parse errors')
    checker = Checker(source, [], str(path))
    config = json.loads(Path(record['config']).read_text())
    identity = signature({'empty_aq': checker.aq_sha256, 'scope': scope,
                          'environment': environment(config, Path(config['project_root']), checker, path)})
    return {'status': 'EMPTY_AQ', 'aq_sha256': checker.aq_sha256, 'cache_identity': identity,
            'mutation_scope': scope, 'jasper_native_percent': None, 'fixed_universe_percent': None,
            'reason': 'Verified empty final AQ; no native Jasper session or mutation counts. '
                      'Native coverage is undefined, not a measured zero.', 'jasper_sessions': 0}
