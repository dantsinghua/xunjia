"""Deployment-only CLI; no HTTP account-assignment endpoint exists."""
import argparse
import os
from .repository import Repository


def main():
    parser=argparse.ArgumentParser(description='Xunjia business database administration')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate',help='Explicitly create isolated xj_* business tables')
    bind=commands.add_parser('bind',help='Assign one fixed account to one portal user')
    bind.add_argument('--owner',required=True);bind.add_argument('--account',required=True)
    bind.add_argument('--workbench',required=True);bind.add_argument('--transport',choices=['oss','mail'],default='oss')
    commands.add_parser('check',help='Check connectivity without exposing credentials')
    args=parser.parse_args()
    url=os.environ.get('XUNJIA_DATABASE_URL','')
    if not url:parser.error('XUNJIA_DATABASE_URL is required')
    repo=Repository(url,initialize=False)
    if args.command=='migrate':repo.initialize_schema();print('Business schema is ready.')
    elif args.command=='bind':repo.bind_account(args.owner,args.account,args.workbench,args.transport);print('Account binding is ready.')
    else:
        with repo.db.transaction(write=False) as tx:tx.execute('SELECT 1').fetchone()
        print('Database connection is ready.')


if __name__=='__main__':main()
