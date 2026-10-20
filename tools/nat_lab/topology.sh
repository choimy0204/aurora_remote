#!/bin/bash
# NAT 실험실 — 네트워크 네임스페이스로 "서로 다른 NAT 뒤의 두 망 + 인터넷" 을 한 PC 안에 만든다 (root 필요)
#
#   hosta(10.1.0.2) ─ nata ─(203.0.113.0/24)─┐
#                                            inet  (안내 서버 · STUN)
#   hostb(10.2.0.2) ─ natb ─(198.51.100.0/24)┘
#
# 사용:  topology.sh up <NAT-A 방식> <NAT-B 방식>   |   topology.sh down
#   방식:  cone      포트 보존 NAT (iptables MASQUERADE — 보내 본 상대의 답만 통과: 홀 펀칭 가능)
#          symmetric 목적지마다 포트가 바뀌는 NAT (MASQUERADE --random — 두 쪽이 모두 이러면 홀 펀칭 불가)
#          udpblock  cone + UDP 는 STUN(3478) 외 모두 차단 (회사 방화벽이 UDP 를 막는 경우)
set -e
NS="inet nata natb hosta hostb"

nat_rules() {   # $1=네임스페이스 $2=공인쪽 인터페이스 $3=방식
    local ns=$1 wan=$2 mode=$3
    ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=1
    # 실제 공유기처럼 WAN 쪽에서 들어온 "요청하지 않은" 패킷은 장비 자신(INPUT)에서 버린다.
    # 이걸 안 하면 먼저 도착한 패킷이 연결 추적 항목을 만들어 뒤따르는 정상 발신의 포트 매핑과 충돌한다(홀 펀칭이 깨짐).
    ip netns exec "$ns" iptables -A INPUT -i "$wan" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ip netns exec "$ns" iptables -A INPUT -i "$wan" -j DROP
    case "$mode" in
        cone)      ip netns exec "$ns" iptables -t nat -A POSTROUTING -o "$wan" -j MASQUERADE ;;
        symmetric) ip netns exec "$ns" iptables -t nat -A POSTROUTING -o "$wan" -j MASQUERADE --random ;;
        udpblock)  ip netns exec "$ns" iptables -t nat -A POSTROUTING -o "$wan" -j MASQUERADE
                   ip netns exec "$ns" iptables -A FORWARD -p udp --dport 3478 -j ACCEPT
                   ip netns exec "$ns" iptables -A FORWARD -p udp --sport 3478 -j ACCEPT
                   ip netns exec "$ns" iptables -A FORWARD -p udp -j DROP ;;
        *) echo "알 수 없는 방식: $mode" >&2; exit 2 ;;
    esac
}

down() {
    for n in $NS; do ip netns del "$n" 2>/dev/null || true; done
}

up() {
    local mode_a="${1:-cone}" mode_b="${2:-cone}"      # 아래 반복문이 위치 인자를 덮어쓰므로 먼저 저장
    down
    for n in $NS; do ip netns add "$n"; ip -n "$n" link set lo up; done
    ip link add i-a type veth peer name na-w; ip link set i-a netns inet;  ip link set na-w netns nata
    ip link add i-b type veth peer name nb-w; ip link set i-b netns inet;  ip link set nb-w netns natb
    ip link add na-l type veth peer name ha-e; ip link set na-l netns nata; ip link set ha-e netns hosta
    ip link add nb-l type veth peer name hb-e; ip link set nb-l netns natb; ip link set hb-e netns hostb
    ip -n inet addr add 203.0.113.1/24 dev i-a;   ip -n nata addr add 203.0.113.10/24 dev na-w
    ip -n inet addr add 198.51.100.1/24 dev i-b;  ip -n natb addr add 198.51.100.10/24 dev nb-w
    ip -n nata addr add 10.1.0.1/24 dev na-l;     ip -n hosta addr add 10.1.0.2/24 dev ha-e
    ip -n natb addr add 10.2.0.1/24 dev nb-l;     ip -n hostb addr add 10.2.0.2/24 dev hb-e
    for pair in "inet i-a" "inet i-b" "nata na-w" "nata na-l" "natb nb-w" "natb nb-l" "hosta ha-e" "hostb hb-e"; do
        read -r ns_name if_name <<< "$pair"; ip -n "$ns_name" link set "$if_name" up
    done
    ip -n hosta route add default via 10.1.0.1
    ip -n hostb route add default via 10.2.0.1
    ip -n nata route add default via 203.0.113.1
    ip -n natb route add default via 198.51.100.1
    ip netns exec inet sysctl -qw net.ipv4.ip_forward=1      # 두 NAT 의 공인 주소끼리는 인터넷(inet)을 거쳐 닿음
    nat_rules nata na-w "$mode_a"
    nat_rules natb nb-w "$mode_b"
}

case "${1:-}" in
    up)   shift; up "$@" ;;
    down) down ;;
    *)    echo "사용: $0 up <cone|symmetric|udpblock> <cone|symmetric|udpblock> | down" >&2; exit 2 ;;
esac
