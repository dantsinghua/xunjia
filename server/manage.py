"""Deployment-only CLI; no HTTP account-assignment endpoint exists."""
import argparse
import os
from .health import schema_health
from .negotiation.application_store import ApplicationStore


def main():
    parser=argparse.ArgumentParser(description='Xunjia business database administration')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate',help='Explicitly create isolated V2.2 xj2_* business tables')
    bind=commands.add_parser('bind',help='Assign one fixed account to one portal user')
    bind.add_argument('--owner',required=True);bind.add_argument('--account',required=True)
    bind.add_argument('--workbench',required=True);bind.add_argument('--transport',choices=['oss','mail'],default='oss')
    commands.add_parser('check',help='Read-only check of the complete V2.2 business schema')
    args=parser.parse_args()
    url=os.environ.get('XUNJIA_DATABASE_URL','')
    if not url:parser.error('XUNJIA_DATABASE_URL is required')
    repo=ApplicationStore(url,initialize=False)
    if args.command=='migrate':repo.initialize();print('V2.2 business schema is ready.')
    elif args.command=='bind':repo.bind_account(args.owner,args.account,args.workbench,args.transport);print('Account binding is ready.')
    else:
        report=schema_health(repo)
        if not report['ready']:raise SystemExit('V2.2 business schema is not ready: '+report['code'])
        print('V2.2 business schema is ready.')


if __name__=='__main__':main()
