"""Known recovery setting is shipped, bounded, and does not enable sends."""
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy'))
from build_offline import template_is_clean
from common import DeployError,validate_config
def test_template_includes_recovery_setting_without_enabling_live_transport():
    values=template_is_clean(ROOT/'config/xunjia.env.example')
    assert values['XUNJIA_VALUATION_REFRESH_SECONDS']=='2'
    assert values['XUNJIA_ENABLE_LIVE_TRANSPORT']=='0'
    assert values['XUNJIA_RUN_BACKGROUND']=='0'
@pytest.mark.parametrize('seconds',['1','2','30','1.5'])
def test_valid_recovery_interval_preserved(seconds):
    values=template_is_clean(ROOT/'config/xunjia.env.example')
    values['XUNJIA_VALUATION_REFRESH_SECONDS']=seconds
    validate_config(values)
    assert values['XUNJIA_VALUATION_REFRESH_SECONDS']==seconds
@pytest.mark.parametrize('seconds',['0','31','nan','inf','oops'])
def test_invalid_recovery_interval_fails_preflight_precisely(seconds):
    values=template_is_clean(ROOT/'config/xunjia.env.example')
    values['XUNJIA_VALUATION_REFRESH_SECONDS']=seconds
    with pytest.raises(DeployError,match='CONFIG_VALUATION_REFRESH_INVALID'):
        validate_config(values)
