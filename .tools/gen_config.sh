#/bin/bash
mkdir -p ~/.paddle
cat > ~/.paddle/config << EOF
datacenters:
- name: datacenter1
  username: gongweibao@baidu.com
  password: T123
  endpoint: http://127.0.0.1:8080
current-datacenter: datacenter1
EOF
