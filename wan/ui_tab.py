"""WAN 탭 화면 — 위젯을 만들고 컨트롤러가 부르는 화면 갱신 함수만 구현하는 얇은 층 (tkinter 는 build_tab() 안에서 불러옴)"""

from . import connect_code
from .config import QUALITY, format_id
from .ui_base import COLUMNS, COLUMN_WIDTHS, FAST_MS, FILE_CHOICES, KEEP, LOG_LINES, NullUI, SLOW_MS, USAGE_HINT, classify_target, is_direct_id
from .ui_controller import WanTabController


# ─────────────────────────────────────────
class WanTab(NullUI):
    def __init__(self, app, parent, ctx, tk, ttk, messagebox, simpledialog, filedialog=None):
        self.app, self.parent, self.ctx = app, parent, ctx
        self.tk, self.ttk, self.mb, self.sd, self.fd = tk, ttk, messagebox, simpledialog, filedialog
        self.root = app.root
        self.px = ctx.get("px") or (lambda v: v)
        self._closed = False
        self._build()
        self.controller = WanTabController(ctx, self._schedule, ui=self)
        self._hook_client()
        self.controller.refresh_all()
        self.root.after(SLOW_MS, self._loop_slow)
        self.root.after(FAST_MS, self._loop_fast)
        parent.bind("<Destroy>", lambda e: self.stop() if e.widget is parent else None, add="+")

    # ── 스레드 · 주기 ──
    def _schedule(self, fn):
        """어느 스레드에서든 fn 을 화면 스레드에서 실행하게 맡김 (창이 이미 없으면 조용히 버림)"""
        def run():
            if self._closed:
                return
            try:
                fn()
            except self.tk.TclError:
                pass
            except Exception as e:
                self._log_error(e)
        try:
            self.root.after(0, run)
        except Exception:
            pass

    def _log_error(self, e):
        try:
            self.controller.log(f"화면 갱신 오류: {type(e).__name__}: {e}")
        except Exception:
            pass

    def _hook_client(self):
        """WanClient.on_change 에 이 탭의 갱신을 덧붙임 (원래 콜백은 그대로 부름)"""
        client = self.ctx["client"]
        prev = getattr(client, "on_change", None)

        def on_change(what):
            if callable(prev):
                prev(what)
            self.controller.on_client_change(what)
        client.on_change = on_change

    def _loop(self, ms, fn, again):
        if self._closed:
            return
        try:
            fn()
        except Exception as e:
            self._log_error(e)
        try:
            self.root.after(ms, again)
        except Exception:
            pass

    def _loop_slow(self):
        self._loop(SLOW_MS, self.controller.tick_slow, self._loop_slow)

    def _loop_fast(self):
        self._loop(FAST_MS, self.controller.tick_fast, self._loop_fast)

    def stop(self):
        self._closed = True

    def log(self, msg: str):
        """작은 로그 창에 한 줄 (어느 스레드에서든 가능)"""
        self.controller.log(msg)

    # ── 위젯 만들기 ──
    def _card_body(self):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.body)
        card.pack(fill="x", pady=(0, px(8)))
        inner = tk.Frame(card, bg=app.PANEL)
        inner.pack(fill="x", padx=px(16), pady=px(10))
        return inner

    def _check(self, parent, text, var, command):
        app, tk = self.app, self.tk
        return tk.Checkbutton(parent, text=text, variable=var, command=command, bg=app.PANEL, fg=app.TEXT,
                              font=app.FONT, activebackground=app.PANEL, selectcolor=app.PANEL, bd=0, anchor="w")

    def _build(self):
        self._build_switch()
        self.body = self.tk.Frame(self.parent, bg=self.app.BG)         # WAN 이 꺼지면 통째로 숨김
        self.body.pack(fill="both", expand=True)
        self._build_list()
        self._build_field()
        self._build_relay()
        self._build_footer()
        self._build_log()

    def _build_switch(self):
        """맨 위 WAN 사용 스위치 — 꺼 두면 아래 칸이 모두 사라지고 WAN 은 아무것도 하지 않는다"""
        app, tk, px = self.app, self.tk, self.px
        self.wan_var = tk.BooleanVar(value=True)
        row = tk.Frame(self.parent, bg=app.BG)
        row.pack(fill="x", pady=(0, px(8)))
        self.wan_check = tk.Checkbutton(row, text="WAN 사용", variable=self.wan_var, command=self._wan_toggled,
                                        bg=app.BG, fg=app.TEXT, font=app.FONT_BOLD, activebackground=app.BG,
                                        selectcolor=app.PANEL, bd=0, anchor="w")
        self.wan_check.pack(side="left")
        app._tip(self.wan_check, "끄면 중계 PC 접속 · 접속 받기 · 목록 확인 · 새 버전 확인 · 접속 정보 가져오기를 모두 하지 않습니다")
        self.wan_off_lbl = tk.Label(row, text="꺼져 있음 — 인터넷으로 아무것도 하지 않습니다", font=app.FONT, fg=app.TEXT_DIM, bg=app.BG)
        if not self.ctx.get("set_wan_enabled"):
            row.pack_forget()                                           # 스위치가 없는 구성은 항상 켜짐
        # 꺼져 있을 때만 보이는 '접속 코드로 시작' — 코드만 넣으면 켜기 · 접속 · 현장 설정까지 알아서 한다
        card = app._card(self.parent)
        self.setup_box = card
        inner = tk.Frame(card, bg=app.PANEL)
        inner.pack(fill="x", padx=px(16), pady=px(12))
        tk.Label(inner, text="접속 코드로 시작", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w").pack(fill="x")
        self.setup_var = tk.StringVar()
        self.setup_field_var = tk.BooleanVar(value=True)
        line = tk.Frame(inner, bg=app.PANEL)
        line.pack(fill="x", pady=(px(6), 0))
        self.setup_entry = app._entry(line, self.setup_var, 40)
        self.setup_entry.pack(side="left", fill="x", expand=True, ipady=px(2))
        self.setup_entry.bind("<Return>", lambda e: self._setup_start())
        self.setup_btn = app._button(line, "시작", "primary", self._setup_start)
        self.setup_btn.pack(side="left", padx=(px(8), 0))
        app._tip(self.setup_btn, "WAN 을 켜고 중계 PC 에 접속합니다. 현장 PC 로 쓰는 경우 이 PC 를 원격 대상으로 켜고 방화벽도 열어 봅니다")
        self._check(inner, "이 PC 를 현장 PC 로 사용 (원격으로 접속 받음)", self.setup_field_var, lambda: None
                    ).pack(anchor="w", pady=(px(4), 0))
        try:
            clip = self.root.clipboard_get()
            if connect_code.kind_of(clip):
                self.setup_var.set(clip.strip())
        except Exception:
            pass

    def _setup_start(self):
        self.controller.quick_setup(self.setup_var.get(), self.setup_field_var.get())

    def _wan_toggled(self):
        self.set_wan_enabled(self.wan_var.get())
        self.controller.wan_toggled(self.wan_var.get())

    def _build_list(self):
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        box = self._card_body()
        self.list_card = box.master                # 원격 대상 PC 로 쓸 때는 이 칸(접속할 PC 목록)을 숨긴다
        head = tk.Frame(box, bg=app.PANEL)
        head.pack(fill="x", pady=(0, px(6)))
        app._section_title(head, "현장 PC").pack(side="left")
        self.server_lbl = tk.Label(head, text="", font=app.FONT, fg=app.TEXT_DIM, bg=app.PANEL, cursor="hand2")
        self.server_lbl.pack(side="right")
        self.server_lbl.bind("<Button-1>", lambda e: self._connect_dialog(self._current_relay()))
        app._tip(self.server_lbl, "중계 PC 연결 상태 — 클릭하면 중계 PC 를 바꿉니다")
        style = ttk.Style()
        style.configure("Wan.Treeview", font=app.FONT_INPUT, rowheight=px(24),
                        background=app.PANEL, fieldbackground=app.PANEL, foreground=app.TEXT,
                        bordercolor=app.BORDER, borderwidth=0)
        style.map("Wan.Treeview", background=[("selected", app.SELECT_BG)],
                  foreground=[("selected", app.TEXT)])
        style.configure("Wan.Treeview.Heading", font=app.FONT_SMALL, background=app.BG,
                        foreground=app.TEXT_DIM, relief="flat", borderwidth=0, padding=(px(6), px(5)))
        wrap = tk.Frame(box, bg=app.PANEL, highlightthickness=1, highlightbackground=app.INPUT_BORDER)
        wrap.pack(fill="x")
        self.tree = ttk.Treeview(wrap, columns=COLUMNS, show="headings", height=4,
                                 selectmode="browse", style="Wan.Treeview")
        for col, w in zip(COLUMNS, COLUMN_WIDTHS):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=px(w), anchor="w", stretch=(col == "이름"))
        self.tree.tag_configure("off", foreground=app.TEXT_DIM)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._connect())
        self.tree.bind("<Return>", lambda e: self._connect())
        self.tree.bind("<Delete>", lambda e: self._forget(quiet=True))
        self.tree.bind("<Button-3>", self._row_menu)
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(8), 0))
        self.connect_btn = app._button(row, "원격 연결", "start", self._connect)
        self.connect_btn.pack(side="left")
        self.add_btn = app._button(row, "+ 연결 추가", "secondary", lambda: self._connect_dialog())
        self.add_btn.pack(side="left", padx=(px(8), 0))
        app._tip(self.add_btn, "중계 PC 접속 정보 · 현장 PC 접속 정보를 붙여넣거나, 현장 PC 주소와 접근 코드를 입력합니다")
        self.config_btn = app._button(row, "설정 보내기", "secondary", self._config_dialog, small=True)
        self.config_btn.pack(side="right")
        self.delete_btn = app._button(row, "삭제", "danger", self._forget, small=True)
        self.delete_btn.pack(side="right", padx=(0, px(6)))
        self.rename_btn = app._button(row, "이름 바꾸기", "secondary", self._rename, small=True)
        self.rename_btn.pack(side="right", padx=(0, px(6)))
        app._tip(self.delete_btn, "선택한 현장을 목록에서 지웁니다 (꺼져 있는 현장 · 주소로 접속했던 현장)")
        app._tip(self.config_btn, "선택한 현장 PC 의 이름·접근 코드·최고 화질·파일 전송을 바꿉니다 (꺼져 있으면 다음 접속 때 적용)")

    def _build_field(self):
        app, tk, px = self.app, self.tk, self.px
        box = self._card_body()
        self.field_card = box.master
        top = tk.Frame(box, bg=app.PANEL)
        top.pack(fill="x")
        self.enabled_var = tk.BooleanVar(value=bool(self.ctx["cfg"].get("enabled")))
        self._check(top, "이 PC를 원격 대상으로 사용", self.enabled_var, self._field_toggled).pack(side="left")
        self.field_lbl = tk.Label(top, text="", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL, anchor="e")
        self.field_lbl.pack(side="right")
        self.field_box = tk.Frame(box, bg=app.PANEL)          # 켰을 때만 보임
        row = tk.Frame(self.field_box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(6), 0))
        app._field_label(row, "주소", width=8).pack(side="left")
        self.address_lbl = tk.Label(row, text="", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w")
        self.address_lbl.pack(side="left")
        copy_btn = app._button(row, "복사", "secondary", lambda: self.controller.copy_field_code(), small=True)
        copy_btn.pack(side="right")
        app._tip(copy_btn, "주소와 접근 코드를 한 줄로 복사 — 접속할 PC 의 '연결 추가'에 붙여넣으면 됩니다")
        row = tk.Frame(self.field_box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        app._field_label(row, "접근 코드", width=8).pack(side="left")
        self.code_var = tk.StringVar(value="")
        self.code_entry = app._entry(row, self.code_var, 24)
        self.code_entry.pack(side="left", fill="x", expand=True, ipady=px(1))
        self.code_entry.bind("<Return>", lambda e: self._apply_code())
        self.code_entry.bind("<FocusOut>", lambda e: self._apply_code())
        app._tip(self.code_entry, "이 PC 에 접속하려면 입력해야 하는 코드. 따로 정하지 않으면 중계 PC 의 공용 키가 쓰입니다")
        info = tk.Frame(self.field_box, bg=app.PANEL)
        info.pack(fill="x", pady=(px(4), 0))
        self.id_lbl = tk.Label(info, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w",
                               cursor="hand2")
        self.id_lbl.pack(side="left")
        relay_btn = app._button(info, "중계 PC 변경", "secondary", lambda: self._connect_dialog(self._current_relay()),
                                small=True)
        relay_btn.pack(side="right")
        app._tip(relay_btn, "이 PC 가 접속할 중계 PC 의 접속 정보를 바꿉니다")
        self.id_lbl.bind("<Button-1>", lambda e: self._copy_id())
        app._tip(self.id_lbl, "클릭하면 이 PC 의 ID 를 복사합니다")
        self.field_warn_lbl = tk.Label(self.field_box, text="", font=app.FONT_SMALL, fg=app.RED, bg=app.PANEL,
                                       anchor="w")
        self._field_layout()

    def _field_layout(self):
        """이 PC 를 원격 대상으로 쓰면 '접속할 PC 목록' 칸은 필요 없으니 숨기고(헷갈리지 않게), 주소 · 접근 코드 칸을 보인다"""
        if self.enabled_var.get():
            self.list_card.pack_forget()
            self.field_box.pack(fill="x")
        else:
            self.field_box.pack_forget()
            self.list_card.pack(fill="x", pady=(0, self.px(8)), before=self.field_card)

    def _build_relay(self):
        """이 PC 가 중계 PC 일 때 — 평소에는 접어 두고, 버튼 한 번으로 안내 서버를 세팅하고 다른 PC 에 줄 접속 정보를 만든다"""
        app, tk, px = self.app, self.tk, self.px
        if not self.ctx.get("relay_available"):
            return
        cfg = self.ctx["cfg"]
        box = self._card_body()
        head = tk.Frame(box, bg=app.PANEL)
        head.pack(fill="x")
        self.relay_open = bool(cfg.get("relay_host"))
        self.relay_toggle_lbl = tk.Label(head, text="", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w",
                                         cursor="hand2")
        self.relay_toggle_lbl.pack(side="left")
        self.relay_toggle_lbl.bind("<Button-1>", lambda e: self._relay_expand(not self.relay_open))
        self.relay_lbl = tk.Label(head, text="", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL)
        self.relay_lbl.pack(side="right")
        self.relay_more = tk.Frame(box, bg=app.PANEL)
        self.relay_var = tk.BooleanVar(value=bool(cfg.get("relay_host")))
        top = tk.Frame(self.relay_more, bg=app.PANEL)
        top.pack(fill="x", pady=(px(8), 0))
        self.auto_btn = app._button(top, "자동 설정", "primary", lambda: self._relay_auto())
        self.auto_btn.pack(side="left")
        app._tip(self.auto_btn, "공용 키 만들기 · 안내 서버 시작 · 방화벽 열기 · 접속 정보 만들기를 한 번에 합니다")
        self._check(top, "서버 켜기", self.relay_var, self._relay_toggled).pack(side="left", padx=(px(10), 0))
        self.admin_btn = app._button(top, "관리자로 다시 시작", "secondary", lambda: self.controller.relaunch_admin(),
                                     small=True)
        code = tk.Frame(self.relay_more, bg=app.PANEL)
        code.pack(fill="x", pady=(px(6), 0))
        app._field_label(code, "접속 정보", width=8).pack(side="left")
        self.relay_code_var = tk.StringVar(value="")
        app._entry(code, self.relay_code_var, 20).pack(side="left", fill="x", expand=True, ipady=px(1))
        app._button(code, "복사", "secondary", lambda: self.controller.copy_relay_code(), small=True
                    ).pack(side="left", padx=(px(6), 0))
        save = app._button(code, "파일 저장", "secondary", lambda: self.controller.save_connection_dialog(), small=True)
        save.pack(side="left", padx=(px(4), 0))
        refresh = app._button(code, "새로고침", "danger", lambda: self.controller.relay_refresh_code(), small=True)
        refresh.pack(side="left", padx=(px(4), 0))
        app._tip(refresh, "접속 코드(공용 키)를 새로 만들어 Git 에 올립니다. 기존 접속 정보는 사라지고 연결 중인 원격 화면은 끊깁니다")
        app._tip(save, "접속 정보를 aurora_wan.conn 파일로 저장 — AuroraClient.exe 와 같은 폴더에 두면 다른 PC 가 실행할 때 자동으로 연결됩니다")
        git = tk.Frame(self.relay_more, bg=app.PANEL)
        git.pack(fill="x", pady=(px(4), 0))
        app._field_label(git, "Git 저장소", width=8).pack(side="left")
        self.git_var = tk.StringVar(value=cfg.get("git_source") or "")
        self.git_entry = app._entry(git, self.git_var, 20)
        self.git_entry.pack(side="left", fill="x", expand=True, ipady=px(1))
        self.git_entry.bind("<Return>", lambda e: self.controller.publish_git(self.git_var.get()))
        up = app._button(git, "올리기", "secondary", lambda: self.controller.publish_git(self.git_var.get()), small=True)
        up.pack(side="left", padx=(px(6), 0))
        app._tip(up, "사내 Git 저장소에 접속 정보를 올립니다. 다른 PC 는 이 저장소 주소를 '+ 연결 추가'에 한 번 넣으면 자동으로 가져옵니다 (접근이 제한된 사내 저장소만 쓰세요)")
        self.git_status_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL,
                                       anchor="w")
        self.git_status_lbl.pack(fill="x")
        opt = tk.Frame(self.relay_more, bg=app.PANEL)
        opt.pack(fill="x", pady=(px(4), 0))
        app._field_label(opt, "외부 주소", width=8).pack(side="left")
        self.relay_ext_var = tk.StringVar(value=cfg.get("relay_external") or "")
        ent = app._entry(opt, self.relay_ext_var, 16)
        ent.pack(side="left", fill="x", expand=True, ipady=px(1))
        ent.bind("<FocusOut>", lambda e: self.controller.set_relay_external(self.relay_ext_var.get()))
        ent.bind("<Return>", lambda e: self.controller.set_relay_external(self.relay_ext_var.get()))
        app._tip(ent, "다른 망(출장지)에서 이 PC 에 닿는 공인 IP 또는 도메인 (선택). 공유기에서 포트포워딩이 필요합니다")
        app._field_label(opt, "포트", width=5).pack(side="left", padx=(px(10), 0))
        self.relay_port_var = tk.StringVar(value=str(cfg.get("relay_port") or 9100))
        app._entry(opt, self.relay_port_var, 6).pack(side="left", ipady=px(1))
        app._button(opt, "방화벽 열기", "secondary", lambda: self.controller.open_firewall(), small=True
                    ).pack(side="left", padx=(px(8), 0))
        self.relay_warn_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.RED, bg=app.PANEL,
                                       anchor="w")
        self._relay_expand(self.relay_open)

    def _relay_expand(self, opened):
        self.relay_open = bool(opened)
        self.relay_toggle_lbl.configure(text=("▾ " if self.relay_open else "▸ ") + "중계 PC 로 사용")
        if self.relay_open:
            self.relay_more.pack(fill="x")
        else:
            self.relay_more.pack_forget()

    def _build_footer(self):
        app, tk, px = self.app, self.tk, self.px
        row = tk.Frame(self.body, bg=app.BG)
        row.pack(fill="x", pady=(0, px(6)))
        self.day_lbl = tk.Label(row, text="오늘 -", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.BG)
        self.day_lbl.pack(side="left")
        self.month_lbl = tk.Label(row, text="이번 달 -", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.BG)
        self.month_lbl.pack(side="left", padx=(px(14), 0))
        app._tip(self.day_lbl, USAGE_HINT)
        app._tip(self.month_lbl, USAGE_HINT)

    def _build_log(self):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.body)
        card.pack(fill="both", expand=True)
        self.log_text = tk.Text(card, height=LOG_LINES, font=app.FONT_MONO_SMALL, bg=app.LOG_BG, fg="#D5DADD",
                                relief="flat", bd=px(6), wrap="none", state="disabled", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)

    # ── 버튼 처리 ──
    def _field_toggled(self):
        self._field_layout()
        self.controller.field_toggled(self.enabled_var.get())
        if self.enabled_var.get() and self.controller.needs_relay_info():
            self._connect_dialog()                  # 현장 PC 로 켰는데 중계 PC 정보가 아직 없으면 바로 물어본다 (체크 + 접속 정보면 끝)

    def _apply_code(self):
        self.controller.set_access_code(self.code_var.get())

    def _relay_auto(self):
        self._relay_expand(True)
        self.controller.relay_auto_setup(self.relay_ext_var.get())

    def _relay_toggled(self):
        self.controller.relay_toggled(self.relay_var.get(), self.relay_port_var.get())

    def _copy_id(self):
        self.copy_text(self.ctx["cfg"].ensure_id(), "이 PC 의 ID 를 복사했습니다")

    def _current_relay(self) -> str:
        return (self.ctx["cfg"].get("relay_url") or "").strip()

    def _selected(self, quiet=False):
        sel = self.tree.selection()
        if not sel:
            if not quiet:
                self.mb.showinfo("현장 PC 선택", "목록에서 현장 PC 를 먼저 선택하세요", parent=self.root)
            return None
        return sel[0]

    def _connect(self):
        row_id = self._selected()
        if row_id:
            self.controller.quick_connect(row_id)

    def _rename(self):
        row_id = self._selected()
        if not row_id:
            return
        agent = self.controller.agent(row_id) or {}
        current = (agent.get("name") or "").strip()
        new = self.sd.askstring("이름 바꾸기", "새 이름을 입력하세요", parent=self.root,
                                initialvalue="" if current == (agent.get("address") or "") else current)
        if new is not None:
            self.controller.rename_target(row_id, new)

    def _forget(self, quiet=False):
        row_id = self._selected(quiet=quiet)
        if row_id:
            self.controller.forget_target(row_id)

    def _row_menu(self, event):
        """목록에서 오른쪽 클릭 — 연결 · 설정 보내기 · 목록에서 지우기"""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        menu = self.tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="원격 연결", command=self._connect)
        if not is_direct_id(row):
            menu.add_command(label="설정 보내기", command=self._config_dialog)
        menu.add_command(label="이름 바꾸기", command=self._rename)
        menu.add_command(label="목록에서 삭제", command=self._forget)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _connect_dialog(self, prefill=""):
        """연결 추가: 중계 PC 접속 정보 · 현장 PC 접속 정보 · 주소(+접근 코드) 어느 것이든 한 칸에 넣는다.
        같은 망이면 중계 PC 를 자동으로 찾아 채운다"""
        app, tk, px = self.app, self.tk, self.px
        cfg = self.ctx["cfg"]
        win = tk.Toplevel(self.root)
        win.title("연결 추가")
        win.configure(bg=app.PANEL)
        win.transient(self.root)
        win.resizable(False, False)
        pad = tk.Frame(win, bg=app.PANEL)
        pad.pack(fill="both", expand=True, padx=px(16), pady=px(12))
        tk.Label(pad, text="접속 정보 또는 현장 PC 주소", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL,
                 anchor="w").pack(fill="x")
        text_var, code_var, field_var = tk.StringVar(), tk.StringVar(), tk.BooleanVar(value=bool(cfg.get("enabled")))
        top = tk.Frame(pad, bg=app.PANEL)
        top.pack(fill="x", pady=(px(6), px(2)))
        entry = app._entry(top, text_var, 44)
        entry.pack(side="left", fill="x", expand=True, ipady=px(2))
        hint = tk.Label(pad, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w")
        hint.pack(fill="x", pady=(0, px(4)))
        code_row = tk.Frame(pad, bg=app.PANEL)                   # 주소 · ws:// 주소를 넣었을 때만
        code_lbl = app._field_label(code_row, "접근 코드", width=8)
        code_lbl.pack(side="left")
        app._entry(code_row, code_var, 24).pack(side="left", fill="x", expand=True, ipady=px(1))
        field_chk = self._check(pad, "이 PC도 원격 대상으로 사용", field_var, lambda: None)     # 중계 PC 정보를 넣었을 때만
        btns = tk.Frame(pad, bg=app.PANEL)
        btns.pack(fill="x", pady=(px(10), 0))

        def refresh(*_):
            kind = classify_target(text_var.get())
            needs_code = kind in ("address", "relay_url")
            if needs_code:
                code_lbl.configure(text="공용 키" if kind == "relay_url" else "접근 코드")
                if not code_var.get():
                    code_var.set((cfg.get("relay_key") or "").strip())
                code_row.pack(fill="x", pady=(px(2), 0), before=btns)
            else:
                code_row.pack_forget()
            if kind in ("relay_code", "relay_url"):
                field_chk.pack(fill="x", pady=(px(4), 0), before=btns)
            else:
                field_chk.pack_forget()
            if kind == "relay_code":
                hint.configure(text="중계 PC 에 연결합니다")
            elif kind == "git":
                hint.configure(text="사내 Git 저장소에서 접속 정보를 가져옵니다 (이후 자동으로 최신 정보를 따라감)")
            elif kind == "direct_code":
                hint.configure(text="현장 PC 에 바로 접속합니다")
            elif kind == "address":
                hint.configure(text="현장 PC 에 바로 접속합니다 (같은 망 · VPN · 포트포워딩된 주소)")

        def pick_file():
            fd = self.fd
            path = fd.askopenfilename(parent=win, initialdir=self.controller.default_conn_dir(),
                                      filetypes=[("Aurora 접속 설정", "*.conn"), ("모든 파일", "*.*")])
            if path:
                text = self.controller.read_connection_file(path)
                if text:
                    text_var.set(text.strip().splitlines()[-1])

        def ok():
            if self.controller.add_connection(text_var.get(), code_var.get(), field_var.get()):
                win.destroy()

        def find():
            hint.configure(text="같은 망에서 중계 PC 를 찾는 중…")
            self.controller.find_on_lan()
        app._button(top, "파일…", "secondary", pick_file, small=True).pack(side="left", padx=(px(6), 0))
        app._button(btns, "연결", "primary", ok).pack(side="left")
        app._button(btns, "취소", "secondary", win.destroy).pack(side="left", padx=(px(8), 0))
        app._button(btns, "같은 망에서 찾기", "secondary", find, small=True).pack(side="right")
        if (cfg.get("git_source") or "").strip():
            app._button(btns, "Git 연동 끄기", "secondary",
                        lambda: (self.controller.set_git_source(""), win.destroy()), small=True
                        ).pack(side="right", padx=(0, px(6)))
        entry.bind("<Return>", lambda e: ok())
        text_var.trace_add("write", refresh)
        self._connect_win = ConnectDialog(win, text_var, code_var, field_var, ok)
        self._connect_win.hint, self._connect_win.btns, self._connect_win.refresh = hint, btns, refresh
        self._connect_win.code_row, self._connect_win.field_chk = code_row, field_chk
        if prefill:
            text_var.set(prefill)
        else:
            try:
                clip = self.root.clipboard_get()
                if connect_code.kind_of(clip):
                    text_var.set(clip.strip())
                    hint.configure(text="복사해 둔 접속 정보를 넣었습니다")
            except Exception:
                pass
        refresh()
        if not text_var.get() and not self._current_relay():
            find()                                                # 아직 중계 PC 가 없으면 같은 망에서 자동으로 찾는다
        try:
            entry.focus_set()
        except Exception:
            pass

    def _config_dialog(self):
        row_id = self._selected()
        if not row_id:
            return
        if is_direct_id(row_id):
            self.controller.send_config(row_id)                  # 주소로 접속한 현장은 보낼 수 없다는 안내가 뜬다
            return
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        agent = self.controller.agent(row_id) or {}
        label = (agent.get("name") or "").strip() or format_id(row_id)
        dlg = tk.Toplevel(self.root)
        dlg.title("설정 보내기")
        dlg.configure(bg=app.PANEL)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=app.PANEL)
        body.pack(fill="both", padx=px(20), pady=px(16))
        tk.Label(body, text=f"'{label}' — 바꿀 항목만 채우세요 (비워 두면 그대로)", font=app.FONT_BOLD,
                 fg=app.TEXT, bg=app.PANEL).pack(anchor="w", pady=(0, px(8)))
        name_var, code_var = tk.StringVar(), tk.StringVar()
        q_var, f_var = tk.StringVar(value=KEEP), tk.StringVar(value=KEEP)
        for text, var, show in (("이름", name_var, None), ("접근 코드", code_var, "*")):
            row = tk.Frame(body, bg=app.PANEL)
            row.pack(fill="x", pady=(0, px(4)))
            app._field_label(row, text, width=10).pack(side="left")
            ent = app._entry(row, var, 28)
            if show:
                ent.configure(show=show)
            ent.pack(side="left", fill="x", expand=True)
        for text, var, values in (("최고 화질", q_var, [KEEP] + list(QUALITY)),
                                  ("파일 전송", f_var, list(FILE_CHOICES))):
            row = tk.Frame(body, bg=app.PANEL)
            row.pack(fill="x", pady=(0, px(4)))
            app._field_label(row, text, width=10).pack(side="left")
            ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=14,
                         font=app.FONT_INPUT).pack(side="left")

        def send():
            quality = q_var.get()
            ok = self.controller.send_config(
                row_id, name_var.get(), code_var.get(), "" if quality == KEEP else quality,
                FILE_CHOICES.get(f_var.get()))
            if ok:
                dlg.destroy()

        btns = tk.Frame(body, bg=app.PANEL)
        btns.pack(fill="x", pady=(px(10), 0))
        app._button(btns, "보내기", "primary", send).pack(side="left")
        app._button(btns, "취소", "secondary", dlg.destroy).pack(side="left", padx=(px(8), 0))
        try:
            dlg.grab_set()
        except Exception:
            pass

    # ── 컨트롤러 → 화면 (화면 스레드에서 불림) ──
    def _color(self, level):
        app = self.app
        return {"ok": app.GREEN, "warn": app.YELLOW, "bad": app.RED}.get(level, app.TEXT_DIM)

    def set_server_status(self, text, level):
        self.server_lbl.configure(text=text, fg=self._color(level))

    def set_rows(self, rows):
        tree = self.tree
        ids = [rid for rid, _v in rows]
        selected = tree.selection()
        tags = lambda values: (() if str(values[2]).startswith("켜짐") else ("off",))
        if list(tree.get_children()) == ids:                # 같은 목록이면 값만 고쳐 선택·스크롤 유지
            for rid, values in rows:
                tree.item(rid, values=values, tags=tags(values))
            return
        for iid in tree.get_children():
            tree.delete(iid)
        for rid, values in rows:
            tree.insert("", "end", iid=rid, values=values, tags=tags(values))
        keep = [s for s in selected if s in ids]
        if keep:
            tree.selection_set(keep)

    def set_field_info(self, id_text, name_text, warn):
        self.id_lbl.configure(text=f"{name_text}  ·  ID {id_text}")
        self._show_warn(self.field_warn_lbl, warn)

    def _show_warn(self, label, text):
        """경고 글자가 있을 때만 줄을 차지하게"""
        label.configure(text=text)
        if text:
            label.pack(fill="x")
        else:
            label.pack_forget()

    def set_field_status(self, text, level):
        self.field_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_field_enabled(self, enabled):
        self.enabled_var.set(bool(enabled))
        self._field_layout()

    def set_field_address(self, text):
        self.address_lbl.configure(text=text)

    def set_field_code(self, text):
        try:
            if self.root.focus_get() is self.code_entry:        # 입력 중인 칸을 덮어쓰지 않는다
                return
        except Exception:
            pass
        if self.code_var.get() != text:
            self.code_var.set(text)

    def set_relay_status(self, text, level):
        if hasattr(self, "relay_lbl"):
            self.relay_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_relay_info(self, port_text, hint, warn):
        if hasattr(self, "relay_lbl"):
            self._show_warn(self.relay_warn_lbl, warn)

    def set_relay_code(self, code):
        if hasattr(self, "relay_code_var") and self.relay_code_var.get() != code:
            self.relay_code_var.set(code)

    def set_relay_git(self, source, status, level):
        if not hasattr(self, "git_var"):
            return
        try:
            typing = self.root.focus_get() is self.git_entry                 # 입력 중인 칸을 덮어쓰지 않는다
        except Exception:
            typing = False
        if not typing and self.git_var.get() != source:
            self.git_var.set(source)
        self.git_status_lbl.configure(text=status, fg=self._color(level))

    def set_relay_enabled(self, enabled):
        if hasattr(self, "relay_var"):
            self.relay_var.set(bool(enabled))
            if enabled:
                self._relay_expand(True)

    def set_admin(self, is_admin):
        if hasattr(self, "admin_btn"):
            if is_admin:
                self.admin_btn.master.pack_forget()      # _button 은 테두리 프레임에 담겨 있어 프레임을 숨겨야 함
            else:
                self.admin_btn.pack(side="left", padx=(self.px(10), 0))

    def set_found_code(self, code):
        d = getattr(self, "_connect_win", None)
        try:
            if d is not None and d.win.winfo_exists():
                if code and not d.text.get():
                    d.text.set(code)
                d.hint.configure(text="같은 망에서 중계 PC 를 찾았습니다" if code else "같은 망에서 중계 PC 를 찾지 못했습니다")
        except self.tk.TclError:
            pass

    def ask_yes_no(self, title, message, on_answer):
        on_answer(bool(self.mb.askyesno(title, message, parent=self.root)))

    def ask_code(self, row_id, label, why, on_answer):
        msg = f"'{label}' 의 접근 코드를 입력하세요" + (f"\n({why})" if why else "")
        on_answer(self.sd.askstring("접근 코드", msg, show="*", parent=self.root,
                                    initialvalue=self.controller.remembered_code(row_id)))

    def ask_save_path(self, directory, filename, on_answer):
        path = self.fd.asksaveasfilename(parent=self.root, initialdir=directory, initialfile=filename,
                                         defaultextension=".conn",
                                         filetypes=[("Aurora 접속 설정", "*.conn"), ("모든 파일", "*.*")])
        on_answer(path or None)

    def copy_text(self, text, done_msg):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.controller.log(done_msg)
        except Exception:
            pass

    def set_wan_enabled(self, enabled):
        self.wan_var.set(bool(enabled))
        if enabled:
            self.wan_off_lbl.pack_forget()
            self.setup_box.pack_forget()
            self.body.pack(fill="both", expand=True)
        else:
            self.body.pack_forget()
            self.wan_off_lbl.pack(side="left", padx=(self.px(10), 0))
            self.setup_box.pack(fill="x", pady=(0, self.px(8)))

    def set_usage(self, day_text, month_text):
        self.day_lbl.configure(text=day_text)
        self.month_lbl.configure(text=month_text)

    def set_log(self, lines):
        t = self.log_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "\n".join(lines))
        t.see("end")
        t.configure(state="disabled")

    def set_busy(self, what, busy):
        btn = {"server": self.add_btn, "connect": self.connect_btn, "config": self.config_btn}.get(what)
        if btn:
            btn.configure(state="disabled" if busy else "normal")

    def show_error(self, title, message):
        self.mb.showerror(title, message, parent=self.root)

    def show_info(self, title, message):
        self.mb.showinfo(title, message, parent=self.root)



class ConnectDialog:
    """연결 추가 창의 입력값 묶음 (시험이 창을 조작할 수 있게 보관)"""
    def __init__(self, win, text, code, field, ok):
        self.win, self.text, self.code, self.field, self.ok = win, text, code, field, ok


def build_tab(app, parent, ctx) -> WanTab:
    """aurora_client 의 탭 만들기에서 부름.  tkinter 는 여기서 불러온다 (이 파일 import 만으로는 필요 없음)"""
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog, filedialog
    return WanTab(app, parent, ctx, tk, ttk, messagebox, simpledialog, filedialog)
