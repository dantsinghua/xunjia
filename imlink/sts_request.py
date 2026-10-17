"""Generate a one-minute STS request from environment secrets, without calling it."""
import argparse
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import time
from .sts_credentials import DEFAULT_STS_URL, STSCredentials, build_access_token


def main():
    parser=argparse.ArgumentParser(description='Generate a private curl config for an STS request')
    parser.add_argument('--output',required=True,help='New private curl config file; existing files are never overwritten')
    args=parser.parse_args()
    key=os.environ.get('XUNJIA_STS_AUTH_KEY','')
    sm4=os.environ.get('XUNJIA_STS_SM4_KEY','').encode('utf-8')
    url=os.environ.get('XUNJIA_STS_URL',DEFAULT_STS_URL)
    # Constructing validates configuration without requesting a credential.
    STSCredentials(auth_key=key,sm4_key=sm4,url=url)
    stamp=int(time.time());token=build_access_token(key,sm4,stamp)
    path=Path(args.output)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as output:
        output.write('url = '+json.dumps(url)+'\nheader = "access_token: '+token+'"\nrequest = "GET"\nsilent\nshow-error\n')
    print('Private request file: '+str(path))
    print('Token window ends approximately: '+datetime.fromtimestamp(stamp+60,timezone.utc).isoformat())
    print('No STS request was sent. Do not commit or share the request file.')


if __name__=='__main__':main()
