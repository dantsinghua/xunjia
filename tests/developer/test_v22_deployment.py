"""Deploy the same V2 business engine whose tests are shipped in the archive."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
from common import validate_config
from build_offline import input_files, template_is_clean


def test_background_analysis_configuration_does_not_require_live_sending():
    values = {'XUNJIA_DATABASE_URL': 'oceanbase://synthetic:synthetic@127.0.0.1/xunjia_test',
              'XUNJIA_SSO_SECRET': 'synthetic', 'XUNJIA_AUTH_SECRET': 'x' * 32,
              'XUNJIA_RUN_BACKGROUND': '1',
              'XUNJIA_DIFY_URL': 'http://127.0.0.1:8090/v1',
              'XUNJIA_DIFY_ANALYSIS_APP_KEY': 'synthetic-a',
              'XUNJIA_DIFY_REPLY_APP_KEY': 'synthetic-b',
              'XUNJIA_AGGREGATION_PROFILE': '/tmp/synthetic-measured-profile.json'}
    assert validate_config(values, ready=True) == []


def test_package_contains_current_core_and_entrypoint_regressions():
    files = input_files(ROOT)
    assert 'tests/cutover/test_production_flow.py' in files
    assert 'tests/negotiation/test_commands.py' in files
    assert 'app/server/negotiation/validation.py' in files


def test_clean_deployment_template_exposes_two_stage_keys_and_measurement():
    values = template_is_clean(ROOT / 'config/xunjia.env.example')
    assert values['XUNJIA_DIFY_ANALYSIS_APP_KEY'] == ''
    assert values['XUNJIA_DIFY_REPLY_APP_KEY'] == ''
    assert values['XUNJIA_AGGREGATION_PROFILE'] == ''
