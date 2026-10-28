from pathlib import Path

from deploy.common import effective_env


def test_fresh_quote_connection_uses_only_new_explicit_business_dsn():
    values = effective_env({
        'XUNJIA_DATABASE_URL': 'oceanbase://user%40tenant%23cluster:p%40ss%3Aword@db.internal:2881/xunjia_business',
        'XUNJIA_QUOTE_SOURCE': 'qb',
        'QB_MYSQL_HOST': '', 'QB_MYSQL_PORT': '', 'QB_MYSQL_USER': '',
        'QB_MYSQL_PASSWORD': '', 'QB_MYSQL_DB': '', 'QB_CONFIG_DIR': '',
    }, Path('/home/new-inquiry'), Path('/home/new-inquiry/app'))
    assert {k: values[k] for k in ('QB_MYSQL_HOST', 'QB_MYSQL_PORT', 'QB_MYSQL_USER', 'QB_MYSQL_PASSWORD', 'QB_MYSQL_DB')} == {
        'QB_MYSQL_HOST': 'db.internal', 'QB_MYSQL_PORT': '2881',
        'QB_MYSQL_USER': 'user@tenant#cluster', 'QB_MYSQL_PASSWORD': 'p@ss:word',
        'QB_MYSQL_DB': 'xunjia_business',
    }
    assert values['QB_CONFIG_DIR'] == '/home/new-inquiry/config/innodealing'


def test_explicit_quote_connection_is_not_silently_replaced():
    values = effective_env({'XUNJIA_DATABASE_URL': 'oceanbase://u:p@business:2881/xunjia_business',
                            'XUNJIA_QUOTE_SOURCE': 'qb', 'QB_MYSQL_HOST': 'explicit-different'},
                           Path('/home/new-inquiry'), Path('/home/new-inquiry/app'))
    assert values['QB_MYSQL_HOST'] == 'explicit-different'
