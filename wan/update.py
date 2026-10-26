"""
새 버전 내려받기 — 안내 서버(update 폴더)에서 AuroraClient.exe 를 받아 검증까지만 한다
=====================================================================================
- 이 모듈은 "받기 + 검증" 만 한다. 실행 중인 exe 를 바꿔치는 일(aurora_update.apply)은 본 프로그램의 몫이다.
  그래서 aurora_update / aurora_client / tkinter 를 import 하지 않는다.
- 버전이 더 새로운지는 안내 서버가 판단한다 (update_info 의 available). 여기서는 다시 비교하지 않는다.
- 받는 도중에는 dest_dir/AuroraClient.exe.part 에만 쓴다. 크기와 sha256 이 모두 맞을 때만
  최종 이름으로 바꾸고 update_ready.json 표식을 남긴다. 하나라도 어긋나면 .part 를 지우고 오류로 돌려준다.
- check_and_download / download 는 예외를 밖으로 내보내지 않는다. 결과는 항상 dict:
      status  "none"       안내 서버에 올려 둔 새 버전이 없음 (update 폴더/version.json 없음)
              "latest"     이미 최신 (서버 버전이 더 높지 않음)
              "downloaded" 받아서 검증까지 끝남 → version · path · sha256 · size
              "error"      실패 (연결 · 키 · 중간 끊김 · 검증 불일치 등)
      msg     화면에 그대로 보여 줄 수 있는 한국어 문장
"""

import asyncio
import hashlib
import json
import os
import threading
import time

EXE_NAMES   = {"client": "AuroraClient.exe", "server": "AuroraServer.exe"}
MARKER_NAME = "update_ready.json"
PROGRESS_EVERY = 0.1        # 진행 알림 최소 간격(초)
HASH_BLOCK     = 1 << 20


class _Fail(Exception):
    """예상한 실패 — 메시지를 그대로 사용자에게 보여 준다"""


def _result(status: str, msg: str, **extra) -> dict:
    return {"status": status, "msg": msg, **extra}


def default_dir(cfg) -> str:
    """내려받은 새 버전을 두는 폴더 — 설정 폴더 아래 wan_update"""
    return os.path.join(cfg.dir, "wan_update")


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(HASH_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _marker_path(dest_dir: str) -> str:
    return os.path.join(dest_dir, MARKER_NAME)


# ─────────────────────────────────────────
# 받아 둔 새 버전 확인 / 치우기
# ─────────────────────────────────────────
def pending_update(dest_dir: str) -> dict | None:
    """받아서 검증까지 끝난 새 버전이 아직 그대로 있으면 표식 내용을, 아니면 None.

    표식이 있어도 파일이 없어졌거나 크기·sha256 이 달라졌으면(중간에 바뀜) 쓸 수 없으므로
    None 을 돌려주고 낡은 표식은 지운다.
    """
    marker = _marker_path(dest_dir)
    try:
        with open(marker, "r", encoding="utf-8-sig") as f:
            info = json.load(f)
        name = os.path.basename(str(info["path"]).replace("\\", "/"))
        if name not in EXE_NAMES.values():
            raise ValueError("알 수 없는 파일 이름")
        path = os.path.join(dest_dir, name)       # 폴더가 옮겨져도 맞도록 표식의 폴더 부분은 쓰지 않음
        if os.path.getsize(path) != int(info["size"]):
            raise ValueError("크기가 다름")
        if _file_sha256(path) != str(info["sha256"]).lower():
            raise ValueError("sha256 이 다름")
        return {**info, "path": path}
    except FileNotFoundError:
        if os.path.exists(marker):                # 표식은 있는데 exe 가 없는 경우
            _remove(marker)
        return None
    except Exception:
        _remove(marker)
        return None


def clear_pending(dest_dir: str):
    """받아 둔 새 버전과 표식, 못 끝낸 .part 를 모두 지운다 (적용을 마쳤거나 포기할 때)"""
    _remove(_marker_path(dest_dir))
    for name in EXE_NAMES.values():
        _remove(os.path.join(dest_dir, name))
        _remove(os.path.join(dest_dir, name + ".part"))


# ─────────────────────────────────────────
# 내려받기
# ─────────────────────────────────────────
def _read_json(raw) -> dict:
    if isinstance(raw, bytes):
        raise _Fail("안내 서버 응답이 올바르지 않습니다 (예상하지 못한 데이터)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise _Fail("안내 서버 응답이 올바르지 않습니다") from None
    if not isinstance(data, dict):
        raise _Fail("안내 서버 응답이 올바르지 않습니다")
    return data


def _check_error(data: dict):
    if data.get("t") == "error":
        if data.get("reason") == "key":
            raise _Fail("안내 서버 키가 맞지 않습니다 (WAN 설정의 안내 서버 키를 확인하세요)")
        raise _Fail(f"안내 서버가 오류를 알려 왔습니다 ({data.get('reason', '')})")


async def _recv(ws, timeout: float):
    """timeout 안에 다음 메시지를 받는다. 키가 틀려 서버가 닫은 경우도 여기서 알려 준다"""
    from websockets.exceptions import ConnectionClosed
    try:
        return await asyncio.wait_for(ws.recv(), timeout)
    except asyncio.TimeoutError:
        raise _Fail(f"안내 서버 응답이 {timeout:g}초 동안 없어 중단했습니다") from None
    except ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        if code == 4001:
            raise _Fail("안내 서버 키가 맞지 않습니다 (WAN 설정의 안내 서버 키를 확인하세요)") from None
        raise _Fail("받는 도중 안내 서버와 연결이 끊어졌습니다") from None


def _parse_info(info: dict) -> tuple:
    try:
        size = int(info["size"])
        sha = str(info["sha256"]).lower()
        version = str(info["version"])
        if size < 0 or len(sha) != 64 or int(sha, 16) < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise _Fail("안내 서버가 보낸 새 버전 정보가 올바르지 않습니다") from None
    return version, size, sha


async def download(url, key, current_ver, dest_dir, kind="client", progress=None, timeout=60.0, pin="") -> dict:
    """안내 서버에서 새 버전이 있으면 dest_dir 로 받아 검증한다. 예외는 내지 않고 결과 dict 로 돌려준다.

    progress(done, total) : 받는 동안 가끔 불린다 (done 은 늘기만 한다).
    timeout               : 연결/응답/조각 사이에 이만큼 아무것도 안 오면 중단 (전체 시간 제한이 아님).
    """
    part = final = None
    completed = False
    try:
        if kind not in EXE_NAMES:
            raise _Fail(f"알 수 없는 종류입니다: {kind}")
        url = str(url or "").strip()
        if not url:
            raise _Fail("안내 서버 주소가 비어 있습니다 (WAN 설정에서 먼저 입력하세요)")
        if "://" not in url:
            url = "ws://" + url
        os.makedirs(dest_dir, exist_ok=True)
        final = os.path.join(dest_dir, EXE_NAMES[kind])
        part = final + ".part"

        from . import tls
        try:
            ws = await tls.connect_ws(url, pin, max_size=2 ** 20, compression=None, ping_interval=20, ping_timeout=20,
                                      open_timeout=timeout)
        except tls.PinMismatch as e:
            raise _Fail(str(e)) from None
        except Exception as e:
            raise _Fail(f"안내 서버에 연결하지 못했습니다 ({url}) — {type(e).__name__}") from None
        try:
            return await _fetch(ws, key, current_ver, kind, dest_dir, final, part, progress, timeout)
        finally:
            try:
                await ws.close()
            except Exception:
                pass
    except _Fail as e:
        return _result("error", str(e))
    except OSError as e:
        return _result("error", f"파일을 저장하지 못했습니다: {e}")
    except Exception as e:
        return _result("error", f"새 버전을 받는 중 오류가 났습니다: {type(e).__name__}: {e}")
    finally:
        # 성공하면 _fetch 가 .part 를 이미 최종 이름으로 바꿨으므로 남는 것은 실패한 찌꺼기뿐
        if part:
            _remove(part)


async def _fetch(ws, key, current_ver, kind, dest_dir, final, part, progress, timeout) -> dict:
    await ws.send(json.dumps({"t": "hello", "role": "update", "key": key or "", "kind": kind,
                              "ver": str(current_ver or "0")}))
    info = _read_json(await _recv(ws, timeout))
    _check_error(info)
    if info.get("t") != "update_info":
        raise _Fail("안내 서버 응답이 올바르지 않습니다")
    if not info.get("available"):
        if info.get("reason") == "none" or "version" not in info:
            return _result("none", "안내 서버에 올려 둔 새 버전이 없습니다")
        return _result("latest", f"이미 최신 버전입니다 (현재 {current_ver}, 서버 {info.get('version')})")

    version, size, sha = _parse_info(info)

    # 새로 받기 시작하면 이전 표식·파일은 믿을 수 없으므로 먼저 치운다 (받다 죽어도 표식이 남지 않게)
    _remove(_marker_path(dest_dir))
    _remove(final)

    await ws.send(json.dumps({"t": "get"}))
    done = 0
    h = hashlib.sha256()
    last_report = 0.0

    def report(force=False):
        nonlocal last_report
        now = time.monotonic()
        if progress and (force or now - last_report >= PROGRESS_EVERY):
            last_report = now
            try:
                progress(done, size)
            except Exception:
                pass            # 화면 쪽 오류가 내려받기를 망치지 않게

    report(force=True)
    end_sha = None
    with open(part, "wb") as f:
        while True:
            msg = await _recv(ws, timeout)
            if isinstance(msg, bytes):
                done += len(msg)
                if done > size:
                    raise _Fail("받은 크기가 안내 서버가 알린 크기보다 큽니다")
                f.write(msg)
                h.update(msg)
                report()
                continue
            data = _read_json(msg)
            _check_error(data)
            if data.get("t") == "end":
                end_sha = str(data.get("sha256", "")).lower()
                break
        f.flush()
        os.fsync(f.fileno())

    # ── 검증: 바이트 수 · 받으며 계산한 sha · 디스크에 쓰인 파일의 sha · 서버가 알린 sha 가 모두 같아야 한다 ──
    if done != size:
        raise _Fail(f"파일이 다 오지 않았습니다 ({done:,} / {size:,} 바이트)")
    if end_sha != sha or h.hexdigest() != sha:
        raise _Fail("받은 파일의 체크섬이 맞지 않아 버렸습니다 (전송 중 손상)")
    if os.path.getsize(part) != size or await asyncio.to_thread(_file_sha256, part) != sha:
        raise _Fail("저장된 파일을 다시 확인하니 체크섬이 맞지 않아 버렸습니다 (디스크 오류 가능)")

    os.replace(part, final)
    marker = {"version": version, "path": final, "sha256": sha, "size": size, "time": time.time(), "kind": kind}
    tmp = _marker_path(dest_dir) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _marker_path(dest_dir))
    except OSError:
        _remove(tmp)
        _remove(final)          # 표식 없는 최종 파일은 남기지 않음
        raise
    report(force=True)
    return _result("downloaded", f"새 버전 {version} 을 받았습니다", version=version, path=final, sha256=sha, size=size)


def check_and_download(cfg, current_ver, kind="client", dest_dir=None, progress=None) -> dict:
    """download() 의 블로킹 버전 — 어느 스레드에서 불러도 된다 (자기 이벤트 루프를 새로 만들어 돈다).

    cfg: WanConfig (relay_url · relay_key 를 쓴다).  dest_dir 를 안 주면 default_dir(cfg).
    """
    try:
        dest_dir = dest_dir or default_dir(cfg)
        args = (cfg.get("relay_url"), cfg.get("relay_key"), current_ver, dest_dir, kind, progress, 60.0,
                cfg.get("relay_fp") or "")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(download(*args))
        # 이미 이벤트 루프가 도는 스레드에서 불렸다면 asyncio.run 이 안 되므로 별도 스레드에서 돌린다
        box = []
        t = threading.Thread(target=lambda: box.append(asyncio.run(download(*args))), daemon=True)
        t.start()
        t.join()
        return box[0]
    except Exception as e:
        return _result("error", f"새 버전 확인 중 오류가 났습니다: {type(e).__name__}: {e}")
