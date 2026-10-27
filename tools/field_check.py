"""현장 PC(Windows)에서 실행 — 이 PC 의 화면 캡처 경로와 JPEG / WebP 크기 · 걸리는 시간 · CPU 를 잰다.
    python tools/field_check.py            (저장소 루트에서, 약 15초. 그동안 마우스를 움직이거나 창을 바꿔 보면 더 잘 보인다)
출력 예) 캡처 경로: DXGI(빠름) | 한 장 읽기 12ms | JPEG 85KB 3ms | WebP 38KB 23ms | 조각(글자 하나) JPEG 1.3KB / WebP 0.4KB
'호환(GDI)' 이 나오면 이 PC 는 빠른 화면 복제를 못 써서 초당 1~2장이 한계다 (원격 데스크톱으로 접속한 상태 · 잠금 화면 · 일부 가상 환경)"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aurora_remote as ar  # noqa: E402


def main():
    if os.name != "nt":
        print("Windows 에서 실행하세요")
        return 1
    print(f"WebP 지원: {ar.WEBP_OK}")
    host = ar.RemoteHost(log=lambda m: None)
    last, seq_cost, frames, cpu0, t0 = -1, [], 0, time.process_time(), time.time()
    sizes = {"jpeg": [], "webp": []}
    path = "?"
    while time.time() - t0 < 15:
        for fmt in ("", "webp"):
            q = {"sid": "check", "mon": -1, "w": 1280, "h": 720, "q": 60, "last": last, "wan": 1, "rough": 0, "fmt": fmt}
            t = time.perf_counter()
            meta, body = ar._unpack(host.frame({k: str(v) for k, v in q.items()}, "점검"))
            ms = (time.perf_counter() - t) * 1000
            path = "호환(GDI, 느림)" if meta.get("gdi") else "DXGI(빠름)"
            if body:
                sizes["webp" if fmt else "jpeg"].append((len(body), ms, meta.get("base") is not None))
                frames += 1
        last = meta.get("seq", last)
        time.sleep(0.1)
    cpu = (time.process_time() - cpu0) / (time.time() - t0) * 100

    def avg(kind, patch):
        xs = [(n, ms) for n, ms, p in sizes[kind] if p == patch]
        return (sum(n for n, _ in xs) / len(xs) / 1024, sum(m for _, m in xs) / len(xs), len(xs)) if xs else None
    print(f"캡처 경로: {path} | 이 점검의 CPU {cpu:.1f}% (한 코어 기준)")
    for label, patch in (("전체 화면", False), ("바뀐 조각", True)):
        j, w = avg("jpeg", patch), avg("webp", patch)
        if j and w:
            print(f"{label:<8} JPEG {j[0]:6.1f}KB {j[1]:6.1f}ms | WebP {w[0]:6.1f}KB {w[1]:6.1f}ms  (n={j[2]}/{w[2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
