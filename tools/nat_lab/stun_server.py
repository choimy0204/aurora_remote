"""최소 STUN 서버 (RFC 5389 Binding) — NAT 실험실의 "인터넷" 쪽에서 돈다.
요청을 보낸 곳의 공인 주소:포트(= NAT 가 바꿔 놓은 값)를 XOR-MAPPED-ADDRESS 로 알려 준다."""

import asyncio
import sys

from aioice import stun


class Server(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = stun.parse_message(data)
        except ValueError:
            return
        if msg.message_method != stun.Method.BINDING or msg.message_class != stun.Class.REQUEST:
            return
        reply = stun.Message(stun.Method.BINDING, stun.Class.RESPONSE, msg.transaction_id,
                             {"XOR-MAPPED-ADDRESS": (addr[0], addr[1])})
        self.transport.sendto(bytes(reply), addr)
        print(f"STUN {addr[0]}:{addr[1]}", flush=True)


async def main(port: int):
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(Server, local_addr=("0.0.0.0", port))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3478))
