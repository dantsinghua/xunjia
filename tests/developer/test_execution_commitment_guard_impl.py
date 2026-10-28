"""Execution promises never become a quote-only handoff notification."""
import pytest

from dify.negotiation_v22.build import build
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.text_roles import public_language_error
from tests.developer.test_handoff_notice_impl import pure_context, plan


UNSAFE = [
    '\u5148\u6267\u884c\u3002',
    '\u6211\u73b0\u5728\u6267\u884c\u8fd9\u7b14\u4ea4\u6613\u3002',
    '\u6536\u5230\uff0c\u6211\u6765\u6267\u884c\u3002',
    '\u4e0d\u6210\u4ea4\uff0c\u5148\u6267\u884c\u3002',
    '\u6211\u4e0d\u80fd\u4e0d\u6267\u884c\u3002',
    '\u6211\u4e0d\u662f\u4e0d\u6267\u884c\u3002',
    '\u6211\u73fe\u5728\u57f7\u884c\u3002',
]
SAFE = [
    '\u6536\u5230\uff0c\u6211\u786e\u8ba4\u4e00\u4e0b\u3002',
    '\u6536\u5230\uff0c\u6211\u8f6c\u4eba\u5de5\u786e\u8ba4\u3002',
    '\u6211\u4e0d\u4f1a\u6267\u884c\u4ea4\u6613\uff0c\u8f6c\u4eba\u5de5\u786e\u8ba4\u3002',
    '\u4e0d\u81ea\u52a8\u6267\u884c\uff0c\u8f6c\u4eba\u5de5\u786e\u8ba4\u3002',
]


@pytest.mark.parametrize('text', UNSAFE)
def test_execution_promises_are_rejected_by_shared_and_handoff_guards(text):
    assert public_language_error(text) is not None
    with pytest.raises(ValueError, match='execution'):
        render_reply_plan(pure_context(), plan(text))


@pytest.mark.parametrize('text', SAFE)
def test_noncommitting_confirmation_and_explicit_refusal_remain_valid(text):
    assert public_language_error(text) is None
    assert render_reply_plan(pure_context(), plan(text))['proposal']['kind'] == 'HANDOFF_NOTICE'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_embedded_dsl_has_identical_execution_and_negation_guards(version):
    document = build('reply', version=version, provider='synthetic', model='synthetic')
    code = next(node['data']['code'] for node in document['workflow']['graph']['nodes'] if node['id'] == 'contract')
    namespace = {}
    exec(compile(code, '<embedded-guard>', 'exec'), namespace)
    for text in UNSAFE:
        with pytest.raises(ValueError, match='execution'):
            namespace['validate_handoff_notice_text'](text)
    for text in SAFE:
        namespace['validate_handoff_notice_text'](text)
