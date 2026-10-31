"""Build the customer's DSL 0.6.0 A/B applications from current contracts."""
from pathlib import Path
import yaml
from dify.negotiation_v22.build import build as build_contract


def build(stage):
    document=build_contract(stage,version='1.17.1',
        provider='langgenius/openai_api_compatible/openai_api_compatible',model='DeepSeek-V4')
    document['version']='0.6.0'
    return document


if __name__=='__main__':
    for stage in ('analysis','reply'):
        Path(__file__).with_name(stage+'.yml').write_text(
            yaml.safe_dump(build(stage),allow_unicode=True,sort_keys=False),encoding='utf8')
