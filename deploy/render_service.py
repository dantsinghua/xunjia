"""Print a dedicated systemd unit for operator review; never install it."""
from pathlib import Path
import argparse,sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import safe_prefix
p=argparse.ArgumentParser();p.add_argument('--prefix',default='/home/xunjia')
a=p.parse_args();prefix=safe_prefix(a.prefix)
if any(c.isspace() for c in str(prefix)):raise SystemExit('Systemd example requires a prefix without whitespace')
print(f'''[Unit]
Description=Xunjia Agent (independent of njcb-all)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
Group=root
WorkingDirectory={prefix}/app
ExecStart={prefix}/app/deploy/start.sh
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
''')