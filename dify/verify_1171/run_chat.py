"""Capture an isolated real Dify SSE turn while exercising the production client."""
import argparse
import json
import sys
import urllib.request

from dify.verify_1171.console_client import Console,ROOT
from server.dify_client import DifyChatClient,DifyClientError


class RecordedResponse:
    def __init__(self,response,path):
        self.response=response;self.status=response.status;self.headers=response.headers
        self.file=path.open('wb');path.chmod(0o600)
    def read1(self,size):
        value=self.response.read1(size);self.file.write(value);self.file.flush();return value
    def __enter__(self):return self
    def __exit__(self,*args):
        self.file.close();return self.response.__exit__(*args)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--turn',type=int,choices=[1,2],default=1);parser.add_argument('--label',default='chat');args=parser.parse_args()
    if not args.label.replace('-','').isalnum():raise ValueError('invalid evidence label')
    c=Console();key=json.loads((ROOT/'app-key.json').read_text())['token'];path=ROOT/(args.label+'.sse')
    def opener(request,timeout):
        return RecordedResponse(urllib.request.urlopen(request,timeout=timeout),path)
    client=DifyChatClient(c.base,key,opener=opener,timeout=90,total_timeout=180)
    old=json.loads((ROOT/'chat-turn-1.json').read_text()) if args.turn==2 else None
    try:
        result=client.chat(query='合成询价，请查询价差并询问可做数量。',user='verify-owner-one',
            conversation_id=old['conversation_id'] if old else None,
            inputs={'schema_version':'1','context_epoch':str(args.turn),'turn_ref':'verify-turn-ref-'+str(args.turn),'service_capability':'fixture-capability-only'},response_mode='streaming')
        out=dict(proposal=result.proposal,conversation_id=result.conversation_id,message_id=result.message_id,task_id=result.task_id,workflow_run_id=result.workflow_run_id)
        target=ROOT/('chat-turn-'+str(args.turn)+'.json');target.write_text(json.dumps(out,ensure_ascii=False,indent=2));target.chmod(0o600)
        print(json.dumps({'success':True,'conversation_id':result.conversation_id,'turn':args.turn,'proposal_epoch':result.proposal['conversation_epoch'],'public_message':result.proposal['public_message'],'workflow_run_id':result.workflow_run_id},ensure_ascii=False))
    except DifyClientError as error:
        print('CLIENT_REJECTED',error.code)
        for block in path.read_text().split('\n\n'):
            if not block.startswith('data: '):continue
            try:event=json.loads(block[6:])
            except ValueError:continue
            data=event.get('data',{})
            if event.get('event')=='error' or data.get('status') in ('failed','exception'):
                text=json.dumps({'event':event.get('event'),'node_id':data.get('node_id'),'error':data.get('error') or event.get('message')},ensure_ascii=False)
                for value in c.secret.values():text=text.replace(value,'[REDACTED]')
                print(text)
        sys.exit(1)


if __name__=='__main__':main()
