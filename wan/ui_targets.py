"""컨트롤러 일부 — 현장 PC 목록 · 접속 · 이름 바꾸기/삭제 · 설정 보내기 · 사용량"""

from . import connect_code
from .config import QUALITY, format_id
from .protocol import WanDenied, WanError
from .usage import fmt_bytes
from .ui_base import DIRECT_ID, NAME_MAX, PROBE_TIMEOUT, is_direct_id, relative_time, validate_code


class TargetsMixin:
    # ── 3. 현장 PC 목록 ──
    def direct_targets(self) -> dict:
        return dict(self.cfg.get("direct_targets") or {})

    def rows_with_ids(self) -> list:
        """목록 행 → [(행 id, 표시용 글자 튜플)].  켜진 PC 먼저, 그 안에서는 이름순.
        행 id: 안내 서버를 거치는 현장은 현장 ID, 주소로 접속했던 현장은 'd:주소:포트'"""
        now = self._clock()
        out = []
        for a in list(self.client.agents or []):
            aid = str(a.get("id", ""))
            online = bool(a.get("online"))
            name = (a.get("name") or "").strip() or "(이름 없음)"
            applied = int(a.get("applied_rev") or 0) >= int(a.get("rev") or 0)
            state = ("켜짐" if online else "꺼짐") + ("" if applied else " · 설정 대기")
            out.append((not online, name.casefold(), aid, name, aid,
                        (name, "중계", state, "지금" if online else relative_time(a.get("last_seen"), now))))
        for address, t in self.direct_targets().items():
            reach = self._reach.get(address)
            online = reach is True
            name = (t.get("name") or "").strip() or address
            out.append((not online, name.casefold(), DIRECT_ID + address, name, DIRECT_ID + address,
                        (name, "주소", "켜짐" if online else "꺼짐" if reach is False else "확인 중",
                         "지금" if online else relative_time(t.get("last"), now))))
        out.sort(key=lambda r: r[:3])
        same = {}
        for r in out:
            same[r[3].casefold()] = same.get(r[3].casefold(), 0) + 1
        rows = []
        for _o, _n, _k, name, rid, values in out:
            if same[name.casefold()] > 1 and not is_direct_id(rid):          # 이름이 겹치면 ID 앞 4자로 구분
                values = (f"{name} ({format_id(rid)[:4]})",) + values[1:]
            rows.append((rid, values))
        return rows

    def rows(self) -> list:
        return [values for _rid, values in self.rows_with_ids()]

    def agent(self, row_id: str):
        """행 id → 현장 정보 dict (id · name …).  주소로 접속했던 현장도 같은 모양으로 만들어 줌"""
        if is_direct_id(row_id):
            address = str(row_id)[len(DIRECT_ID):]
            t = self.direct_targets().get(address)
            if t is None:
                return None
            return {"id": row_id, "name": (t.get("name") or "").strip() or address, "address": address, "direct": True}
        for a in list(self.client.agents or []):
            if a.get("id") == row_id:
                return dict(a)
        return None

    def refresh_agents(self):
        self._ui("set_rows", self.rows_with_ids())

    def probe_targets(self):
        """주소로 접속했던 현장이 지금 닿는지 확인해 목록의 켜짐/꺼짐을 갱신 (작업 스레드)"""
        targets = list(self.direct_targets())
        if not targets or self._probing or not self.wan_on():
            return
        self._probing = True
        check = self.ctx.get("reachable") or (lambda a: connect_code.reachable(a, PROBE_TIMEOUT))

        def work():
            try:
                for address in targets:
                    try:
                        self._reach[address] = bool(check(address))
                    except Exception:
                        self._reach[address] = False
            finally:
                self._probing = False
            self.refresh_agents()
        self._spawn(work)

    def forget_target(self, row_id: str) -> bool:
        """목록에서 지우기.  주소로 접속했던 현장은 바로 지우고, 중계 PC 의 현장은 (꺼져 있을 때만) 확인을 받아 중계 PC 목록에서 지운다"""
        if not is_direct_id(row_id):
            return self._forget_relay(row_id)
        targets = self.direct_targets()
        if targets.pop(str(row_id)[len(DIRECT_ID):], None) is None:
            return False
        self.cfg.update(direct_targets=targets)
        self._codes.pop(row_id, None)
        self.refresh_agents()
        return True

    def rename_target(self, row_id: str, name) -> bool:
        """목록의 이름 바꾸기.  주소로 접속했던 현장은 이 PC 에만 저장하고, 중계 PC 의 현장은 '설정 보내기'와 같은 경로로 현장에 보낸다
        (꺼져 있으면 다음 접속 때 적용되고, 중계 PC 목록에는 바로 바뀐 이름이 보인다)"""
        name = (name or "").strip()
        if not name:
            self._ui("show_error", "이름 바꾸기", "이름을 입력하세요")
            return False
        if len(name) > NAME_MAX:
            self._ui("show_error", "이름 바꾸기", f"이름은 {NAME_MAX}자 이하여야 합니다")
            return False
        if not is_direct_id(row_id):
            if self.agent(row_id) is None:
                self._ui("show_error", "이름 바꾸기", "목록에서 현장 PC 를 먼저 선택하세요")
                return False
            return self.send_config(row_id, name=name)
        address = str(row_id)[len(DIRECT_ID):]
        targets = self.direct_targets()
        if address not in targets:
            return False
        targets[address] = dict(targets[address], name=name)
        self.cfg.update(direct_targets=targets)
        self.refresh_agents()
        return True

    def _forget_relay(self, agent_id: str) -> bool:
        agent = self.agent(agent_id)
        if agent is None:
            return False
        if agent.get("online"):
            self._ui("show_info", "삭제", "켜져 있는 현장은 지울 수 없습니다 — 현장 PC 를 끈 뒤 지우세요")
            return False
        label = self._label(agent_id, agent)
        self._ui("ask_yes_no", "현장 삭제",
                 f"'{label}' 을(를) 중계 PC 의 현장 목록에서 지울까요?\n(이 중계 PC 를 쓰는 모든 PC 의 목록에서 사라집니다. 그 PC 가 다시 켜지면 새로 나타납니다)",
                 lambda yes: self._do_forget_relay(agent_id, label) if yes else None)
        return True

    def _do_forget_relay(self, agent_id: str, label: str):
        def work():
            try:
                self.client.forget_agent(agent_id)
            except Exception as e:
                text = str(e) or type(e).__name__
                self._note(f"{label} 삭제 실패: {text}")
                self._ui("show_error", "삭제", text)
            else:
                self._saved_forget(agent_id)
                self._note(f"{label} 을(를) 목록에서 지웠습니다")
                self.refresh_agents()
        self._spawn(work)

    def _saved_forget(self, agent_id: str):
        self._forget_code(agent_id)
        self._codes.pop(agent_id, None)

    def on_client_change(self, what):
        """WanClient.on_change — 클라이언트 스레드에서 불림.  화면 갱신은 화면 스레드로"""
        if what == "agents":
            self.refresh_agents()
        elif what == "state":
            self.refresh_server()
            self.refresh_agents()

    def remembered_code(self, row_id: str) -> str:
        if is_direct_id(row_id):
            saved = (self.direct_targets().get(str(row_id)[len(DIRECT_ID):]) or {}).get("code", "")
            return self._codes.get(row_id) or saved
        return self._codes.get(row_id) or (self.cfg.get("saved_codes") or {}).get(row_id, "")

    def default_code(self, row_id: str) -> str:
        """더블클릭 때 쓸 코드: 중계 PC 가 알려 준 현장의 코드 → 한 번 입력해 둔 코드 → 없으면 공용 키"""
        if not is_direct_id(row_id):
            given = ((self.agent(row_id) or {}).get("code") or "").strip()      # 중계 PC 가 현장의 접근 코드를 알려 줌
            if given:
                return given
        return self.remembered_code(row_id) or (self.cfg.get("relay_key") or "").strip()

    def quick_connect(self, row_id: str) -> bool:
        """더블클릭 / '원격 연결': 코드를 묻지 않고 바로 연결. 코드가 안 맞을 때만 한 번 물어본다"""
        if not self._need_wan():
            return False
        agent = self.agent(row_id)
        if agent is None:
            self._ui("show_error", "원격 연결", "목록에서 현장 PC 를 먼저 선택하세요")
            return False
        code = self.default_code(row_id)
        if not code:
            self._ask_code(row_id, agent, "")
            return True
        if agent.get("direct"):
            return self._connect_address(agent["address"], code, ask_on_deny=True, validate=False)
        return self.connect(row_id, code, ask_on_deny=True, validate=False)

    def _label(self, row_id, agent):
        return (agent.get("name") or "").strip() or format_id(row_id)

    def _ask_code(self, row_id, agent, why):
        retry = ((lambda code: self._connect_address(agent["address"], code)) if agent.get("direct")
                 else (lambda code: self.connect(row_id, code)))
        self._ui("ask_code", row_id, self._label(row_id, agent), why,
                 lambda code: code is not None and retry(code))

    def _save_code(self, agent_id, code):
        """공용 키가 아닌, 따로 입력한 코드만 기억 (다음부터 더블클릭만으로 연결)"""
        if code == (self.cfg.get("relay_key") or "").strip():
            return
        saved = dict(self.cfg.get("saved_codes") or {})
        saved[agent_id] = code
        self.cfg.update(saved_codes=saved)

    def _forget_code(self, agent_id):
        saved = dict(self.cfg.get("saved_codes") or {})
        if saved.pop(agent_id, None) is not None:
            self.cfg.update(saved_codes=saved)

    def connect(self, agent_id: str, code, ask_on_deny=False, validate=True) -> bool:
        """중계 PC 를 거치는 현장에 연결 (작업 스레드).  성공하면 ctx["open_remote"](session, agent) 를 화면 스레드에서 부름.
        ask_on_deny: 코드가 맞지 않으면 오류 대신 접근 코드를 물어봄"""
        agent = self.agent(agent_id)
        if agent is None:
            self._ui("show_error", "원격 연결", "목록에서 현장 PC 를 먼저 선택하세요")
            return False
        name = self._label(agent_id, agent)
        return self._run_connect(agent_id, agent, name, code, lambda c: self.client.open_session(agent_id, c, name),
                                 ask_on_deny, validate, on_ok=lambda c, s: self._save_code(agent_id, c))

    def _connect_address(self, address: str, code, ask_on_deny=False, validate=True, fp="") -> bool:
        """현장 PC 의 주소로 바로 접속 (안내 서버 없이)"""
        direct = self.ctx.get("direct")
        if direct is None:
            self._ui("show_error", "연결 추가", "이 프로그램에서는 주소로 접속할 수 없습니다")
            return False
        row_id = DIRECT_ID + address
        agent = self.agent(row_id) or {"id": row_id, "name": address, "address": address, "direct": True}
        known = "" if agent["name"] == address else agent["name"]              # 이름을 모르면 현장이 알려 주는 이름을 쓴다
        pin = fp or (self.direct_targets().get(address) or {}).get("fp", "")      # 알고 있는 인증서 지문으로 고정
        return self._run_connect(row_id, agent, agent["name"], code,
                                 lambda c: direct.open_session(address, c, known, pin),
                                 ask_on_deny, validate, on_ok=lambda c, s: self._remember_target(address, c, s))

    def _remember_target(self, address, code, session):
        targets = self.direct_targets()
        old = targets.get(address) or {}
        name = (getattr(session, "name", "") or old.get("name") or "").strip()
        targets[address] = {"name": name if name != address else "", "code": code, "last": self._clock(),
                            "fp": getattr(session, "fp", "") or old.get("fp", "")}
        self.cfg.update(direct_targets=targets)
        self._reach[address] = True
        self.refresh_agents()

    def _run_connect(self, row_id, agent, name, code, opener, ask_on_deny, validate, on_ok) -> bool:
        if validate:
            code, err = validate_code(code)
            if err:
                self._ui("show_error", "원격 연결", err)
                return False
        if row_id in self._connecting:
            self.log("이미 연결하는 중입니다")
            return False
        self._connecting.add(row_id)
        self._codes[row_id] = code
        self._ui("set_busy", "connect", True)
        self.log(f"{name} 에 연결하는 중…")

        def work():
            retry = False
            try:
                session = opener(code)
            except WanDenied as e:
                self._codes.pop(row_id, None)      # 틀린 코드는 다음에 미리 채우지 않음
                if not is_direct_id(row_id):
                    self._forget_code(row_id)
                if ask_on_deny and "맞지 않" in str(e):
                    retry = True
                    self._note(f"{name}: 접근 코드가 필요합니다")
                else:
                    self._fail(name, e)
            except WanError as e:
                self._fail(name, e)
            except Exception as e:
                self._fail(name, f"{type(e).__name__}: {e}")
            else:
                on_ok(code, session)
                self._note(f"{name} 연결됨 ({getattr(session, 'mode', '')})")
                agent_ = self.agent(row_id) or agent
                self.schedule(lambda: self.ctx["open_remote"](session, agent_))
            finally:
                self._connecting.discard(row_id)
                self._ui("set_busy", "connect", bool(self._connecting))
            if retry:
                self._ask_code(row_id, agent, "이 현장 PC 는 따로 접근 코드를 정해 두었습니다")
        self._spawn(work)
        return True

    def _fail(self, name, err):
        text = str(err) or "연결하지 못했습니다"
        self._note(f"{name} 연결 실패: {text}")
        self._ui("show_error", "원격 연결 실패", text)

    def send_config(self, agent_id: str, name=None, code=None, max_quality=None, allow_files=None) -> bool:
        """현장 설정 보내기 (작업 스레드).  비어 있는 항목(None / '')은 보내지 않는다"""
        if is_direct_id(agent_id):
            self._ui("show_info", "설정 보내기", "주소로 접속한 현장은 설정을 보낼 수 없습니다 — 그 PC 에서 직접 바꾸세요")
            return False
        cfg, problems = {}, []
        name = (name or "").strip()
        if name:
            if len(name) > NAME_MAX:
                problems.append(f"이름은 {NAME_MAX}자 이하여야 합니다")
            else:
                cfg["name"] = name
        code = (code or "").strip()
        if code:
            code, err = validate_code(code)
            if err:
                problems.append(err)
            else:
                cfg["access_code"] = code
        if max_quality:
            if max_quality in QUALITY:
                cfg["max_quality"] = max_quality
            else:
                problems.append("최고 화질이 올바르지 않습니다")
        if allow_files is not None:
            cfg["allow_files"] = bool(allow_files)
        if problems:
            self._ui("show_error", "설정 보내기", "\n".join(problems))
            return False
        if not cfg:
            self._ui("show_error", "설정 보내기", "보낼 항목이 없습니다 — 바꿀 항목만 채워 주세요")
            return False
        agent = self.agent(agent_id)
        label = ((agent or {}).get("name") or "").strip() or format_id(agent_id)
        self._ui("set_busy", "config", True)
        self.log(f"{label} 에 설정 보내는 중 ({', '.join(cfg)})")

        def work():
            try:
                reply = self.client.set_config(agent_id, cfg)
            except Exception as e:
                text = str(e) or type(e).__name__
                self._note(f"{label} 설정 보내기 실패: {text}")
                self._ui("show_error", "설정 보내기 실패", text)
            else:
                if "access_code" in cfg:
                    self._codes[agent_id] = cfg["access_code"]
                if reply.get("online"):
                    msg = f"{label} 설정 적용됨"
                else:
                    msg = f"{label} 는 꺼져 있어 다음 접속 때 적용됩니다"
                self._note(msg)
                self._ui("show_info", "설정 보내기", msg)
                self.refresh_agents()
            finally:
                self._ui("set_busy", "config", False)
        self._spawn(work)
        return True

    # ── 4. 데이터 사용량 ──
    def usage_texts(self) -> tuple:
        return (f"오늘 {fmt_bytes(self.usage.day_total())}",
                f"이번 달 {fmt_bytes(self.usage.month_total())}")

    def usage_text(self) -> str:
        return " · ".join(self.usage_texts())

    def refresh_usage(self):
        self._ui("set_usage", *self.usage_texts())

    # ── 주기 갱신 ──
    def refresh_all(self):
        self._ui("set_wan_enabled", self.wan_on())
        self.refresh_server()
        self.refresh_relay()
        self.refresh_agents()
        self.refresh_field()
        self.refresh_usage()
        self.probe_targets()

    def tick_slow(self):
        self.refresh_server()
        self.refresh_agents()
        self.probe_targets()

    def tick_fast(self):
        self.check_git_alert()
        self.refresh_relay()
        self.refresh_usage()
        self.refresh_field(reload=True)


# ─────────────────────────────────────────
# 화면 (얇은 위젯 층)
