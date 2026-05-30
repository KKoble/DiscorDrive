import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import asyncio
import os
import math
import json
import re
import time
from pathlib import Path
import discord
from discord.ext import commands
import io

CONFIG_FILE = "config.json"
CHUNK_SIZE = 8 * 1024 * 1024
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"token": "", "channel_id": ""}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

class DriveBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.ready_event = threading.Event()

    async def on_ready(self):
        print(f"봇 로그인: {self.user}")
        self.ready_event.set()

bot = DriveBot()
bot_loop = None
bot_thread = None

def start_bot(token):
    global bot_loop, bot_thread
    bot_loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(bot_loop)
        try:
            bot_loop.run_until_complete(bot.start(token))
        except Exception as e:
            print(f"봇 오류: {e}")

    bot_thread = threading.Thread(target=run, daemon=True)
    bot_thread.start()
    return bot.ready_event.wait(timeout=15)

async def upload_file_async(channel_id: int, filepath: str, save_name: str, progress_cb):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    file_size = os.path.getsize(filepath)
    total_chunks = math.ceil(file_size / CHUNK_SIZE)
    ext = Path(filepath).suffix
    orig_base = save_name if save_name else Path(filepath).stem
    import re as _re
    clean_base = _re.sub(r'[^A-Za-z0-9_\-]', '_', orig_base)

    with open(filepath, "rb") as f:
        for i in range(total_chunks):
            chunk_data = f.read(CHUNK_SIZE)
            chunk_num = str(i + 1).zfill(2)
            filename = f"{clean_base}{ext}.{chunk_num}"
            await channel.send(
                content=f"📦 `{filename}` ({i+1}/{total_chunks})",
                file=discord.File(io.BytesIO(chunk_data), filename=filename)
            )
            progress_cb(i + 1, total_chunks)
            await asyncio.sleep(0.3)

    await channel.send(
        f"✅ **UPLOAD_COMPLETE** `{clean_base}{ext}` ORIG=`{orig_base}{ext}` CHUNKS={total_chunks} SIZE={file_size}"
    )

async def list_files_async(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    files = {}  
    async for msg in channel.history(limit=500):
        if msg.content.startswith("✅ **UPLOAD_COMPLETE**"):
            m = re.search(r"`(.+?)`(?:\s+ORIG=`(.+?)`)?\s+CHUNKS=(\d+)(?:\s+SIZE=(\d+))?", msg.content)
            if m:
                clean_name = m.group(1) 
                orig_name  = m.group(2) if m.group(2) else clean_name  
                total = int(m.group(3))
                size_bytes = int(m.group(4)) if m.group(4) else total * CHUNK_SIZE
                clean_base = Path(clean_name).stem
                ext_part   = Path(clean_name).suffix
                orig_base  = Path(orig_name).stem
                if size_bytes >= 1024 ** 3:
                    size_str = f"{size_bytes / 1024**3:.1f} GB"
                elif size_bytes >= 1024 ** 2:
                    size_str = f"{size_bytes / 1024**2:.1f} MB"
                else:
                    size_str = f"{size_bytes / 1024:.1f} KB"
                files[clean_name] = {
                    "name": orig_name,         
                    "clean_name": clean_name, 
                    "base": clean_base,       
                    "orig_base": orig_base,  
                    "ext": ext_part,
                    "total_chunks": total,
                    "size_str": size_str,
                    "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M"),
                }
    return list(files.values())

async def download_file_async(channel_id: int, file_info: dict, dest_dir: str, progress_cb):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    base = file_info["base"]          
    orig_base = file_info.get("orig_base", base)  
    ext = file_info["ext"]
    total = file_info["total_chunks"]
    chunks = {}

    async for msg in channel.history(limit=2000):
        for att in msg.attachments:
         
            pattern = re.compile(rf"^{re.escape(base)}{re.escape(ext)}\.(\d+)$")
            m = pattern.match(att.filename)
            if m:
                idx = int(m.group(1))
                chunks[idx] = att

    if len(chunks) < total:
        raise Exception(f"청크 부족: {len(chunks)}/{total} 찾음")

    out_path = os.path.join(dest_dir, f"{orig_base}{ext}")
    with open(out_path, "wb") as out:
        for i in range(1, total + 1):
            data = await chunks[i].read()
            out.write(data)
            progress_cb(i, total)
            await asyncio.sleep(0.05)
    return out_path

async def delete_file_async(channel_id: int, file_info: dict, progress_cb=None):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    base       = file_info["base"]
    ext        = file_info["ext"]
    clean_name = file_info.get("clean_name", f"{base}{ext}")
    att_pattern = re.compile(rf"^{re.escape(base)}{re.escape(ext)}\.(\d+)$")

    chunk_txt_pattern = re.compile(
        rf"^📦`{re.escape(base)}{re.escape(ext)}\.\d+`"
    )

    to_delete = []
    async for msg in channel.history(limit=2000):
        matched = False
        for att in msg.attachments:
            if att_pattern.match(att.filename):
                matched = True
                break
        if not matched and chunk_txt_pattern.match(msg.content):
            matched = True
        if not matched and msg.content.startswith("✅ **UPLOAD_COMPLETE**"):
            marker_m = re.search(r"`([^`]+)`", msg.content)
            if marker_m and marker_m.group(1) == clean_name:
                matched = True

        if matched:
            to_delete.append(msg)

    total_found = len(to_delete)
    deleted = 0
    for msg in to_delete:
        try:
            await msg.delete()
            deleted += 1
        except Exception:
            pass
        if progress_cb:
            progress_cb(deleted, total_found)
        await asyncio.sleep(0.2)

    return deleted

class DiscordDriveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Discord Drive")
        self.root.geometry("780x620")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(True, True)

        self.cfg = load_config()
        self.connected = False
        self.selected_file_path = None
        self.file_list_data = []

        self._build_styles()
        self._build_ui()

    def _build_styles(self):
        self.BG       = "#1a1a2e"
        self.PANEL    = "#16213e"
        self.ACCENT   = "#5865F2"   
        self.ACCENT2  = "#57F287"   
        self.TEXT     = "#DCDDDE"
        self.SUBTEXT  = "#72767d"
        self.CARD     = "#0f3460"
        self.DANGER   = "#ED4245"

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Custom.TFrame", background=self.PANEL)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("Custom.TLabel",
            background=self.PANEL, foreground=self.TEXT,
            font=("Segoe UI", 10))
        style.configure("Title.TLabel",
            background=self.BG, foreground=self.TEXT,
            font=("Segoe UI", 18))
        style.configure("Sub.TLabel",
            background=self.BG, foreground=self.SUBTEXT,
            font=("Segoe UI", 9))
        style.configure("Custom.TEntry",
            fieldbackground="#2b2d31", foreground=self.TEXT,
            bordercolor=self.ACCENT, insertcolor=self.TEXT,
            font=("Segoe UI", 10))
        style.configure("Accent.TButton",
            background=self.ACCENT, foreground="white",
            font=("Segoe UI", 10),
            borderwidth=0, relief="flat")
        style.map("Accent.TButton",
            background=[("active", "#4752c4"), ("pressed", "#3c45a5")])
        style.configure("Green.TButton",
            background=self.ACCENT2, foreground="#1a1a2e",
            font=("Segoe UI", 10),
            borderwidth=0, relief="flat")
        style.map("Green.TButton",
            background=[("active", "#3ba55c")])
        style.configure("Danger.TButton",
            background=self.DANGER, foreground="white",
            font=("Segoe UI", 10),
            borderwidth=0, relief="flat")
        style.configure("Custom.Treeview",
            background="#2b2d31", foreground=self.TEXT,
            fieldbackground="#2b2d31", rowheight=30,
            font=("Segoe UI", 10))
        style.configure("Custom.Treeview.Heading",
            background=self.CARD, foreground=self.TEXT,
            font=("Segoe UI", 10))
        style.map("Custom.Treeview",
            background=[("selected", self.ACCENT)])
        style.configure("Custom.Horizontal.TProgressbar",
            troughcolor="#2b2d31", background=self.ACCENT,
            borderwidth=0, thickness=8)

    def _build_ui(self):
        title_frame = tk.Frame(self.root, bg=self.BG, pady=10)
        title_frame.pack(fill="x", padx=20)

        tk.Label(title_frame, text="🗄️  Discord Drive",
                 bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 20, "bold")).pack(side="left")

        self.status_dot = tk.Label(title_frame, text="●",
                                   bg=self.BG, fg=self.DANGER,
                                   font=("Segoe UI", 14))
        self.status_dot.pack(side="right", padx=(0, 5))
        self.status_lbl = tk.Label(title_frame, text="연결 안됨",
                                   bg=self.BG, fg=self.SUBTEXT,
                                   font=("Segoe UI", 9))
        self.status_lbl.pack(side="right")

        cfg_frame = tk.LabelFrame(self.root, text=" ⚙ 봇 설정 ",
                                   bg=self.PANEL, fg=self.SUBTEXT,
                                   font=("Segoe UI", 9),
                                   bd=1, relief="groove")
        cfg_frame.pack(fill="x", padx=20, pady=(0, 8))

        row1 = tk.Frame(cfg_frame, bg=self.PANEL)
        row1.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(row1, text="Bot Token", bg=self.PANEL, fg=self.SUBTEXT,
                 font=("Segoe UI", 9), width=12, anchor="w").pack(side="left")
        self.token_var = tk.StringVar(value=self.cfg.get("token", ""))
        token_entry = tk.Entry(row1, textvariable=self.token_var,
                                bg="#2b2d31", fg=self.TEXT,
                                insertbackground=self.TEXT,
                                bd=0, font=("Segoe UI", 10), show="•",
                                relief="flat")
        token_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0,8))

        row2 = tk.Frame(cfg_frame, bg=self.PANEL)
        row2.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(row2, text="Channel ID", bg=self.PANEL, fg=self.SUBTEXT,
                 font=("Segoe UI", 9), width=12, anchor="w").pack(side="left")
        self.channel_var = tk.StringVar(value=self.cfg.get("channel_id", ""))
        ch_entry = tk.Entry(row2, textvariable=self.channel_var,
                             bg="#2b2d31", fg=self.TEXT,
                             insertbackground=self.TEXT,
                             bd=0, font=("Segoe UI", 10),
                             relief="flat")
        ch_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0,8))
        self.connect_btn = tk.Button(cfg_frame,
                                      text="  연결하기  ",
                                      bg=self.ACCENT, fg="white",
                                      font=("Segoe UI", 10),
                                      bd=0, relief="flat",
                                      cursor="hand2",
                                      activebackground="#4752c4",
                                      activeforeground="white",
                                      command=self._connect)
        self.connect_btn.pack(side="right", padx=12, pady=(0, 8))

        up_frame = tk.LabelFrame(self.root, text=" ⬆ 파일 업로드 ",
                                  bg=self.PANEL, fg=self.SUBTEXT,
                                  font=("Segoe UI", 9),
                                  bd=1, relief="groove")
        up_frame.pack(fill="x", padx=20, pady=(0, 8))

        row3 = tk.Frame(up_frame, bg=self.PANEL)
        row3.pack(fill="x", padx=12, pady=(8, 4))

        self.file_path_var = tk.StringVar(value="파일을 선택하세요...")
        tk.Label(row3, textvariable=self.file_path_var,
                 bg="#2b2d31", fg=self.SUBTEXT,
                 font=("Segoe UI", 9),
                 anchor="w", padx=8).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(row3, text=" 📂 파일선택 ",
                  bg="#4f545c", fg=self.TEXT,
                  font=("Segoe UI", 9),
                  bd=0, relief="flat", cursor="hand2",
                  activebackground="#686d73",
                  activeforeground=self.TEXT,
                  command=self._pick_file).pack(side="left", padx=(8, 0))

        row4 = tk.Frame(up_frame, bg=self.PANEL)
        row4.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(row4, text="저장 이름",
                 bg=self.PANEL, fg=self.SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        self.save_name_var = tk.StringVar()
        tk.Entry(row4, textvariable=self.save_name_var,
                 bg="#2b2d31", fg=self.TEXT,
                 insertbackground=self.TEXT,
                 bd=0, font=("Segoe UI", 10),
                 relief="flat").pack(side="left", fill="x", expand=True, ipady=4)
        self.upload_btn = tk.Button(up_frame,
                                     text="  ⬆ 업로드  ",
                                     bg=self.ACCENT2, fg="#1a1a2e",
                                     font=("Segoe UI", 10),
                                     bd=0, relief="flat", cursor="hand2",
                                     activebackground="#3ba55c",
                                     activeforeground="#1a1a2e",
                                     state="disabled",
                                     command=self._upload)
        self.upload_btn.pack(side="right", padx=12, pady=(0, 8))

        prog_frame = tk.Frame(up_frame, bg=self.PANEL)
        prog_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_frame,
                                             variable=self.progress_var,
                                             maximum=100,
                                             style="Custom.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")
        self.progress_lbl = tk.Label(prog_frame, text="",
                                      bg=self.PANEL, fg=self.SUBTEXT,
                                      font=("Segoe UI", 8))
        self.progress_lbl.pack(anchor="e")

        list_frame = tk.LabelFrame(self.root, text=" 📁 저장된 파일 목록 ",
                                    bg=self.PANEL, fg=self.SUBTEXT,
                                    font=("Segoe UI", 9),
                                    bd=1, relief="groove")
        list_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        toolbar = tk.Frame(list_frame, bg=self.PANEL)
        toolbar.pack(fill="x", padx=12, pady=(8, 4))
        tk.Button(toolbar, text=" 🔄 새로고침 ",
                  bg="#4f545c", fg=self.TEXT,
                  font=("Segoe UI", 9),
                  bd=0, relief="flat", cursor="hand2",
                  activebackground="#686d73",
                  activeforeground=self.TEXT,
                  command=self._refresh_list).pack(side="left", padx=(0, 8))
        self.download_btn = tk.Button(toolbar, text=" ⬇ 다운로드 ",
                                       bg=self.ACCENT, fg="white",
                                       font=("Segoe UI", 9),
                                       bd=0, relief="flat", cursor="hand2",
                                       activebackground="#4752c4",
                                       activeforeground="white",
                                       state="disabled",
                                       command=self._download)
        self.download_btn.pack(side="left", padx=(0, 8))
        self.delete_btn = tk.Button(toolbar, text=" 🗑 삭제 ",
                                     bg=self.DANGER, fg="white",
                                     font=("Segoe UI", 9),
                                     bd=0, relief="flat", cursor="hand2",
                                     activebackground="#a12d2f",
                                     activeforeground="white",
                                     state="disabled",
                                     command=self._delete)
        self.delete_btn.pack(side="left")
        self.list_status = tk.Label(toolbar, text="",
                                     bg=self.PANEL, fg=self.SUBTEXT,
                                     font=("Segoe UI", 9))
        self.list_status.pack(side="right")

        cols = ("name", "ext", "size", "chunks", "date")
        self.tree = ttk.Treeview(list_frame, columns=cols,
                                  show="headings",
                                  style="Custom.Treeview",
                                  selectmode="browse")
        self.tree.heading("name", text="파일 이름")
        self.tree.heading("ext", text="확장자")
        self.tree.heading("size", text="용량")
        self.tree.heading("chunks", text="청크 수")
        self.tree.heading("date", text="업로드 날짜")
        self.tree.column("name", width=230, anchor="w")
        self.tree.column("ext", width=70, anchor="center")
        self.tree.column("size", width=90, anchor="center")
        self.tree.column("chunks", width=70, anchor="center")
        self.tree.column("date", width=140, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.log_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.log_var,
                 bg=self.BG, fg=self.SUBTEXT,
                 font=("Segoe UI", 8),
                 anchor="w").pack(fill="x", padx=20, pady=(0, 8))

    def _log(self, msg):
        self.log_var.set(f"  {msg}")

    def _connect(self):
        token = self.token_var.get().strip()
        channel_id = self.channel_var.get().strip()
        if not token or not channel_id:
            messagebox.showwarning("입력 오류", "토큰과 채널 ID를 모두 입력하세요.")
            return
        self.cfg["token"] = token
        self.cfg["channel_id"] = channel_id
        save_config(self.cfg)

        self.connect_btn.config(text="연결 중...", state="disabled")
        self._log("봇 연결 중...")

        def do_connect():
            ok = start_bot(token)
            self.root.after(0, self._on_connected if ok else self._on_connect_fail)

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        self.connected = True
        self.status_dot.config(fg=self.ACCENT2)
        self.status_lbl.config(text=f"연결됨 — {bot.user}")
        self.connect_btn.config(text="연결됨 ✓", state="disabled")
        self.upload_btn.config(state="normal")
        self._log("✅ 봇 연결 성공!")
        self._refresh_list()

    def _on_connect_fail(self):
        self.connect_btn.config(text="연결하기", state="normal")
        self._log("❌ 연결 실패 — 토큰/채널 ID 확인")
        messagebox.showerror("연결 실패", "봇 연결에 실패했습니다.\n토큰과 채널 ID를 확인하세요.")

    def _pick_file(self):
        path = filedialog.askopenfilename(title="업로드할 파일 선택")
        if path:
            self.selected_file_path = path
            name = Path(path).name
            self.file_path_var.set(name)
            stem = Path(path).stem
            self.save_name_var.set(stem)

    def _upload(self):
        if not self.selected_file_path:
            messagebox.showwarning("파일 없음", "먼저 파일을 선택하세요.")
            return
        if not os.path.exists(self.selected_file_path):
            messagebox.showerror("오류", "파일을 찾을 수 없습니다.")
            return

        channel_id = int(self.channel_var.get().strip())
        save_name = self.save_name_var.get().strip()
        filepath = self.selected_file_path

        self.upload_btn.config(state="disabled")
        self.progress_var.set(0)
        self._log(f"업로드 시작: {Path(filepath).name}")

        def progress_cb(done, total):
            pct = done / total * 100
            self.root.after(0, lambda: self.progress_var.set(pct))
            self.root.after(0, lambda: self.progress_lbl.config(
                text=f"{done}/{total} 청크 전송됨"))
            self.root.after(0, lambda: self._log(
                f"업로드 중... {done}/{total} ({pct:.0f}%)"))

        async def _run():
            await upload_file_async(channel_id, filepath, save_name, progress_cb)

        def thread_fn():
            future = asyncio.run_coroutine_threadsafe(_run(), bot_loop)
            try:
                future.result(timeout=600)
                self.root.after(0, self._on_upload_done)
            except Exception as e:
                self.root.after(0, lambda: self._on_upload_error(str(e)))

        threading.Thread(target=thread_fn, daemon=True).start()

    def _on_upload_done(self):
        self.upload_btn.config(state="normal")
        self.progress_var.set(100)
        self._log("✅ 업로드 완료!")
        messagebox.showinfo("완료", "파일 업로드가 완료되었습니다.")
        self._refresh_list()

    def _on_upload_error(self, err):
        self.upload_btn.config(state="normal")
        self._log(f"❌ 업로드 실패: {err}")
        messagebox.showerror("업로드 실패", f"오류:\n{err}")

    def _refresh_list(self):
        if not self.connected:
            return
        channel_id = int(self.channel_var.get().strip())
        self.list_status.config(text="불러오는 중...")
        self._log("파일 목록 불러오는 중...")

        async def _run():
            return await list_files_async(channel_id)

        def thread_fn():
            future = asyncio.run_coroutine_threadsafe(_run(), bot_loop)
            try:
                result = future.result(timeout=60)
                self.root.after(0, lambda: self._populate_list(result))
            except Exception as e:
                self.root.after(0, lambda: self.list_status.config(
                    text=f"오류: {e}"))

        threading.Thread(target=thread_fn, daemon=True).start()

    def _populate_list(self, files):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.file_list_data = files
        for f in files:
            display_name = f.get("orig_base", f["base"])
            self.tree.insert("", "end",
                              values=(display_name, f["ext"], f.get("size_str", "—"), f["total_chunks"], f["timestamp"]))
        self.list_status.config(text=f"{len(files)}개 파일")
        self._log(f"📂 파일 목록 갱신됨 — {len(files)}개")

    def _on_select(self, event):
        sel = self.tree.selection()
        state = "normal" if sel else "disabled"
        self.download_btn.config(state=state)
        self.delete_btn.config(state=state)

    def _download(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        file_info = self.file_list_data[idx]

        dest = filedialog.askdirectory(title="저장할 폴더 선택")
        if not dest:
            return

        channel_id = int(self.channel_var.get().strip())
        self.download_btn.config(state="disabled")
        self.progress_var.set(0)
        self._log(f"다운로드 시작: {file_info['name']}")

        def progress_cb(done, total):
            pct = done / total * 100
            _done, _total, _pct = done, total, pct
            self.root.after(0, lambda p=_pct: self.progress_var.set(p))
            self.root.after(0, lambda d=_done, t=_total, p=_pct: self.progress_lbl.config(
                text=f"{d}/{t} 청크 수신됨 ({p:.0f}%)"))
            self.root.after(0, lambda d=_done, t=_total: self._log(
                f"다운로드 중... {d}/{t}"))

        async def _run():
            return await download_file_async(channel_id, file_info, dest, progress_cb)

        def thread_fn():
            future = asyncio.run_coroutine_threadsafe(_run(), bot_loop)
            try:
                out_path = future.result(timeout=600)
                self.root.after(0, lambda p=out_path: self._on_download_done(p))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._on_download_error(msg))

        threading.Thread(target=thread_fn, daemon=True).start()

    def _on_download_done(self, path):
        self.download_btn.config(state="normal")
        self.progress_var.set(100)
        self._log(f"✅ 다운로드 완료: {path}")
        messagebox.showinfo("완료", f"다운로드 완료!\n저장 위치:\n{path}")

    def _on_download_error(self, err):
        self.download_btn.config(state="normal")
        self._log(f"❌ 다운로드 실패: {err}")
        messagebox.showerror("다운로드 실패", f"오류:\n{err}")

    def _delete(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        file_info = self.file_list_data[idx]
        display = file_info.get("orig_base", file_info["base"]) + file_info["ext"]

        if not messagebox.askyesno("삭제 확인",
                f"'{display}' 을(를) 삭제하시겠습니까?\n"
                "Discord 채널의 청크 메시지가 모두 삭제됩니다."):
            return

        channel_id = int(self.channel_var.get().strip())
        self.delete_btn.config(state="disabled")
        self.download_btn.config(state="disabled")
        self.progress_var.set(0)
        self.progress_lbl.config(text="")
        self._log(f"삭제 중: {display}")

        def del_progress_cb(done, total):
            pct = done / total * 100 if total else 0
            self.root.after(0, lambda p=pct: self.progress_var.set(p))
            self.root.after(0, lambda d=done, t=total, p=pct: self.progress_lbl.config(
                text=f"{d}/{t} 메시지 삭제됨 ({p:.0f}%)"))
            self.root.after(0, lambda d=done, t=total: self._log(
                f"삭제 중... {d}/{t}"))

        async def _run():
            return await delete_file_async(channel_id, file_info, del_progress_cb)

        def thread_fn():
            future = asyncio.run_coroutine_threadsafe(_run(), bot_loop)
            try:
                count = future.result(timeout=300)
                self.root.after(0, lambda c=count, n=display: self._on_delete_done(c, n))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._on_delete_error(msg))

        threading.Thread(target=thread_fn, daemon=True).start()

    def _on_delete_done(self, count, name):
        self.progress_var.set(100)
        self._log(f"✅ 삭제 완료: {name} ({count}개 메시지 삭제)")
        messagebox.showinfo("삭제 완료", f"'{name}' 삭제 완료\n({count}개 메시지 삭제됨)")
        self._refresh_list()

    def _on_delete_error(self, err):
        self.delete_btn.config(state="normal")
        self._log(f"❌ 삭제 실패: {err}")
        messagebox.showerror("삭제 실패", f"오류:\n{err}")

if __name__ == "__main__":
    root = tk.Tk()
    app = DiscordDriveApp(root)
    root.mainloop()