"""Explicit file-gateway probe. Default is non-consuming pending(); never tests STS AK/SK.
Roundtrip is opt-in and requires an exclusively reserved empty test queue.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app" if (ROOT/"app/relay").is_dir() else ROOT))
from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",required=True)
    parser.add_argument("--auth-file",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--allow-http",action="store_true")
    parser.add_argument("--roundtrip",action="store_true")
    parser.add_argument("--exclusive-test-queue",action="store_true")
    args=parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        parser.error("Choose a new evidence directory; never overwrite prior attempts")
    args.output.mkdir(parents=True,mode=0o700)
    record={"passed":False,"native_messages_sent":0,"mode":"roundtrip" if args.roundtrip else "pending","started_at":datetime.now(timezone.utc).isoformat(),"uploads_attempted":0,"downloads_attempted":0}
    def save():
        temporary=args.output/"result.json.new"
        temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2));temporary.chmod(0o600)
        temporary.replace(args.output/"result.json")
    try:
        if args.roundtrip and not args.exclusive_test_queue:
            raise FileGatewayError("FILE_PROBE_EXCLUSIVE_QUEUE_REQUIRED")
        auth=GatewayAuth.from_file(args.auth_file)
        client=FileGatewayClient(args.base_url,auth,allow_http=args.allow_http)
        record["credential_file_valid"]=True
        before=client.pending();record["pending_before"]=before;save()
        if args.roundtrip:
            if before!=0:raise FileGatewayError("FILE_PROBE_QUEUE_NOT_EMPTY")
            nonce=uuid.uuid4().hex;payload=("XUNJIA_SYNTHETIC_FILE_PROBE_ONLY\n"+nonce+"\n").encode()
            name="xunjia-probe-"+nonce+".txt"
            record.update(uploads_attempted=1,expected_sha256=hashlib.sha256(payload).hexdigest());save()
            record["upload"]=client.upload(name,payload);save()
            after=client.pending();record["pending_after_upload"]=after;save()
            if after!=1:raise FileGatewayError("FILE_PROBE_QUEUE_CHANGED_STOP_WITHOUT_DOWNLOAD")
            record["downloads_attempted"]=1;save()
            downloaded=client.download()
            if downloaded is None:raise FileGatewayError("FILE_PROBE_UPLOAD_NOT_FOUND",ambiguous=True)
            # Never use the remote filename as a local path. Preserve unexpected bytes.
            fd=os.open(args.output/"downloaded.bin",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"wb") as stream:
                stream.write(downloaded.content);stream.flush();os.fsync(stream.fileno())
            record["downloaded_sha256"]=hashlib.sha256(downloaded.content).hexdigest()
            record["content_matches"]=downloaded.name==name and downloaded.content==payload;save()
            if not record["content_matches"]:raise FileGatewayError("FILE_PROBE_FOREIGN_FILE_QUARANTINED")
            record["pending_after_download"]=client.pending()
            if record["pending_after_download"]!=0:raise FileGatewayError("FILE_PROBE_QUEUE_CHANGED_STOP_WITHOUT_DOWNLOAD")
            # Do not perform an extra destructive GET to prove the queue empty.
            record["owned_file_removed_from_queue"]=True
        record["passed"]=True
    except FileGatewayError as exc:
        record.update(error_code=exc.code,http_status=exc.status,outcome_unknown=exc.ambiguous)
    except Exception as exc:
        record.update(error_code="FILE_PROBE_FAILED",error_type=type(exc).__name__)
    finally:
        save();print(json.dumps(record,ensure_ascii=False))
    return 0 if record["passed"] else 2

if __name__=="__main__":raise SystemExit(main())
