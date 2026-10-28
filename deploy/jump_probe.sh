#!/usr/bin/env bash
# Run on the existing jump host. No restart, config change or business request.
# Port 30080 uses its actual local listener, not an assumed Internet upstream.
# Relay TLS may require a client certificate; a curl failure is not proof of TCP failure.
if ! ip -o -4 addr show | grep -q ' 172.26.64.36/'; then
  echo 'Run on 172.26.64.36 only'; exit 2
fi
urls='
https://its-relay.yingmi.cn:50443
https://web.innodealing.com:11443
https://rest.innodealing.com:12443
https://bosh2.innodealing.com:13443
http://127.0.0.1:30080
'
a=(-q -I -sS -m 8 -o /dev/null --noproxy '*')
f='http=%{http_code} tcp=%{time_connect} tls=%{time_appconnect}\n'
while :; do
  date '+%F %T %z'
  failed=0
  for u in $urls; do
    hp=${u#*://}
    timeout 5 getent ahostsv4 "${hp%:*}" >/dev/null
    printf '%s ' "$hp"
    curl "${a[@]}" --resolve "$hp:127.0.0.1" "$u" -w "$f"
    [ "$?" = 0 ] || failed=1
  done
  [ "${1:-}" = once ] && exit "$failed"
  sleep 30
done
