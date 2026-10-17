"""Shared red lines for every outgoing branch, including initial inquiry."""
import re

SECRET_TEXT=re.compile(r'底价|底线|我(?:们|方)?(?:的)?(?:最低|最高|极限)|授信|可投量|内部|理财产品|组织架构|剩余空间|system\s*prompt|turn_ref|service_capability',re.I)
DEAL_TEXT=re.compile(r'成交|确认交易|确认买|确认卖|给你了|收了|买定|卖定|就这么定|按这个做|按此做|接受(?:这个|该|此)?(?:价格|报价)|拍板|敲定|deal\b|done\b|done\s+at',re.I)

def reject_commitment_or_secret(text):
    if SECRET_TEXT.search(text) or DEAL_TEXT.search(text):raise ValueError("公开文字涉及敏感信息或成交承诺")
