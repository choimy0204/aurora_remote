"""NAT 실험실 시험 — 서로 다른 NAT 뒤의 두 망에서 실제 WAN 스택(aiortc · websockets)을 돌린다
root 권한의 Linux 에서 ip · iptables · conntrack 이 있어야 하고, 없으면 건너뛴다 (자세한 설명: tools/nat_lab/run_lab.py)"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "nat_lab"))
import run_lab  # noqa: E402

WHY = run_lab.available() if os.name == "posix" else "Linux 전용"
pytestmark = pytest.mark.skipif(WHY is not None, reason=f"NAT 실험실을 쓸 수 없음: {WHY}")


@pytest.mark.parametrize("name", ["cone-cone", "symmetric-symmetric", "udp-blocked", "rebind", "holepunch-script"])
def test_scenario(name):
    fn, (a_mode, b_mode) = run_lab.SCENARIOS[name]
    result = fn(name, a_mode, b_mode)
    problems = run_lab.check(name, result)
    assert not problems, (problems, {k: v for k, v in result.items() if k not in ("agent_log", "user_log")})
