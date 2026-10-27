"""Wine/Windows 에서 진짜 RemoteHost 로: 겹친 요청(2장)의 응답 바이트·CPU 를 pipe 유무로 비교 (tools/wine_lab 의 Wine 으로: wine python tools/wine_lab/pipe_bench.py (저장소 루트에서))"""
import sys, time, tkinter
sys.path[:0] = ["tests", "."]
import winshim
ar = winshim.load_remote(); root = winshim.make_root(); root.geometry("800x500+0+0"); root.attributes("-topmost", True)
cv = tkinter.Canvas(root, bg="black", highlightthickness=0); cv.pack(fill="both", expand=True)
root.update(); time.sleep(0.5)
import random
def run(pipe, secs=8):
    host = ar.RemoteHost(log=lambda m: None); sid = f"b{pipe}"
    q = lambda last: {"sid": sid, "mon": -1, "w": 1400, "h": 1000, "q": 60, "last": last, **({"pipe": 1} if pipe else {})}
    m, _ = ar._unpack(host.frame(q(-1), "bench")); last = m["seq"]
    n = byt = 0; c0 = time.process_time(); end = time.time() + secs; i = 0
    while time.time() < end:
        for _ in range(3):
            x, y = random.randrange(0, 700), random.randrange(0, 400)
            cv.create_rectangle(x, y, x + 90, y + 60, fill="#%06x" % random.randrange(1 << 24), outline="")
        root.update(); i += 1
        a = host.frame(q(last), "bench"); b = host.frame(q(last), "bench")      # 겹쳐 온 두 요청 (같은 last)
        for r in (a, b):
            meta, body = ar._unpack(r); byt += len(r); n += 1
        last = max(ar._unpack(a)[0].get("seq", last), ar._unpack(b)[0].get("seq", last)); time.sleep(0.12)
    return byt / secs / 1024, (time.process_time() - c0) / secs * 100
for pipe in (1, 0, 1, 0):
    kb, cpu = run(pipe); print(f"pipe={pipe}: 응답 {kb:.1f} KB/s, 이 프로세스 CPU {cpu:.1f}% (그림 그리기 포함)")
