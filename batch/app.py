import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from local_security import LocalConsoleSecurity

from automation_engine import (
    GoogleUltraAutomator,
    LocalHttpProxyAuthBridge,
    get_totp,
    get_profile_dir,
    resolve_account_proxy,
    parse_proxy_string,
    CHROME_PATH,
    BASE_DIR,
    SCREENSHOTS_DIR,
)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

app = FastAPI(title="Google AI Pro (5 TB) 零成本两年全自动控制台")
app.add_middleware(LocalConsoleSecurity)
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")

# Global runtime state
LOGS: List[str] = [
    f"[{time.strftime('%H:%M:%S')}] [SYSTEM] 🚀 Google AI Pro (5 TB) 零成本两年 (US$0.00 -> 2028-10) 全自动控制台已就绪！"
]
ACTIVE_TASKS: Dict[str, GoogleUltraAutomator] = {}
BATCH_RUNNING: bool = False
STOP_BATCH: bool = False


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {"target_region": "US", "target_plan": "pro_2year_zero", "auto_cancel": True, "card": {}, "accounts": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: Dict[str, Any]):
    temp_path = None
    try:
        # Write beside the target so replacement cannot expose a partial config.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(CONFIG_PATH),
            prefix=".config-", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def append_log(line: str):
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows launch scripts use CP936, which cannot display every emoji.
        # Keep the original Unicode message in the browser's log buffer.
        encoding = sys.stdout.encoding or "utf-8"
        print(line.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)
    LOGS.append(line)
    if len(LOGS) > 500:
        del LOGS[: len(LOGS) - 500]


class ConfigUpdateReq(BaseModel):
    target_region: str = "US"
    target_plan: str = "pro_2year_zero"
    auto_cancel: bool = True
    proxy_template: str = ""
    card: Dict[str, str]


class ImportAccountsReq(BaseModel):
    raw_text: str


class RunSingleReq(BaseModel):
    account_id: str
    start_step: int = 1


class RunBatchReq(BaseModel):
    account_ids: Optional[List[str]] = None
    start_step: int = 1


class AccountActionReq(BaseModel):
    account_id: str


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(TEMPLATES_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/state")
async def get_state():
    cfg = load_config()
    rem_sec = 30 - (int(time.time()) % 30)
    accounts_out = []
    for acc in cfg.get("accounts", []):
        item = dict(acc)
        try:
            item["totp_now"] = get_totp(acc.get("totp_secret", ""))
        except Exception:
            item["totp_now"] = "------"
        item["totp_rem"] = rem_sec
        item["is_running"] = acc["id"] in ACTIVE_TASKS
        accounts_out.append(item)

    return {
        "target_region": cfg.get("target_region", "US"),
        "target_plan": cfg.get("target_plan", "pro_2year_zero"),
        "auto_cancel": cfg.get("auto_cancel", True),
        "proxy_template": cfg.get("proxy_template", ""),
        "card": cfg.get("card", {}),
        "accounts": accounts_out,
        "logs": LOGS[-250:],
        "batch_running": BATCH_RUNNING,
        "active_count": len(ACTIVE_TASKS),
    }


@app.post("/api/config")
async def update_config(req: ConfigUpdateReq):
    cfg = load_config()
    cfg["target_region"] = req.target_region
    cfg["target_plan"] = req.target_plan
    cfg["auto_cancel"] = req.auto_cancel
    cfg["proxy_template"] = req.proxy_template.strip()
    cfg["card"] = req.card
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [CONFIG] ✅ 已保存配置 (目标地区: {req.target_region} | 代理模板: {'已启用' if req.proxy_template.strip() else '系统默认'})")
    return {"ok": True}


class ProxyTestReq(BaseModel):
    target_region: str = "TR"
    proxy_template: str = ""


@app.post("/api/proxy/test")
async def test_proxy(req: ProxyTestReq):
    cfg = load_config()
    tpl = (req.proxy_template or cfg.get("proxy_template") or "").strip()
    region = (req.target_region or cfg.get("target_region") or "TR").upper()
    resolved = resolve_account_proxy({"id": f"probe{int(time.time())%1000}", "email": "probe@gmail.com"}, region, tpl)
    if not resolved:
        return {"ok": False, "message": "未填写代理模板"}

    def _sync_probe():
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": resolved, "https": resolved}))
        r = urllib.request.Request("https://ipwho.is/", headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(opener.open(r, timeout=15).read().decode("utf-8"))
        bal_gb = None
        api_key = cfg.get("floppy_api_key", "")
        if api_key:
            try:
                rb = urllib.request.Request(
                    "https://api.floppydata.net/v2/account/balances",
                    headers={"X-Api-Key": api_key, "User-Agent": "Mozilla/5.0", "Connection": "close"},
                )
                bdata = json.loads(urllib.request.urlopen(rb, timeout=10).read().decode("utf-8"))
                bal_gb = bdata.get("proxy", {}).get("rotating", {}).get("total", {}).get("traffic", {}).get("availableGb")
            except Exception:
                pass
        return data, bal_gb

    try:
        data, bal_gb = await asyncio.to_thread(_sync_probe)
        ip = data.get("ip", "Unknown")
        cc = data.get("country_code", "??")
        country = data.get("country", "")
        city = data.get("city", "")
        org = data.get("connection", {}).get("org", "")
        flag = "🇧🇴" if cc == "BO" else ("🇹🇷" if cc == "TR" else "🌐")
        bal_str = f" | FloppyData 剩余流量: {bal_gb} GB" if bal_gb is not None else ""
        msg = f"{flag} 连通成功！出口 IP: {ip} ({country} · {city} | {org}){bal_str}"
        append_log(f"[{time.strftime('%H:%M:%S')}] [PROXY] ✅ {msg}")
        return {"ok": True, "message": msg, "ip": ip, "country_code": cc, "balance_gb": bal_gb}
    except Exception as e:
        err = f"代理测试失败: {e}"
        append_log(f"[{time.strftime('%H:%M:%S')}] [PROXY] ❌ {err}")
        return {"ok": False, "message": err}


@app.post("/api/accounts/import")
async def import_accounts(req: ImportAccountsReq):
    cfg = load_config()
    existing = cfg.get("accounts", [])
    existing_emails = {a["email"].lower().replace(".", "") for a in existing}

    lines = [l.strip() for l in req.raw_text.splitlines() if l.strip()]
    added = 0
    for line in lines:
        # Support delimiters: ----, —, |, /, \t, comma, or whitespace
        if "----" in line:
            parts = [p.strip() for p in line.split("----") if p.strip()]
        elif "—" in line:
            parts = [p.strip() for p in line.split("—") if p.strip()]
        elif "|" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
        elif "/" in line and "@" in line and "http://" not in line and "https://" not in line:
            parts = [p.strip() for p in line.split("/") if p.strip()]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
        elif "," in line:
            parts = [p.strip() for p in line.split(",") if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"\s+", line) if p.strip()]

        if len(parts) < 2:
            continue

        # Detect if first part is a label (no '@') or email (has '@')
        if "@" in parts[0]:
            email = parts[0]
            password = parts[1]
            rest = parts[2:]
            acc_id = f"Account_{len(existing) + 1}"
        elif len(parts) >= 3 and "@" in parts[1]:
            acc_id = parts[0]
            email = parts[1]
            password = parts[2]
            rest = parts[3:]
        else:
            continue

        recovery_email = ""
        totp_secret = ""
        proxy_str = ""
        for token in rest:
            t_clean = token.replace(" ", "")
            if "@" in token and not token.lower().startswith(("http://", "https://", "socks5://")):
                if token.lower() != email.lower() and not recovery_email:
                    recovery_email = token.strip()
            elif token.lower().startswith(("http://", "https://", "socks5://")) or re.match(r"^\d+\.\d+\.\d+\.\d+:\d+", token):
                proxy_str = token.strip()
            elif len(t_clean) >= 16 and re.match(r"^[A-Za-z2-7]+=*$", t_clean) and not totp_secret:
                totp_secret = t_clean

        norm_email = email.lower().replace(".", "")
        if norm_email in existing_emails:
            for a in existing:
                if a["email"].lower().replace(".", "") == norm_email:
                    a["password"] = password
                    if recovery_email:
                        a["recovery_email"] = recovery_email
                    if totp_secret:
                        a["totp_secret"] = totp_secret
                    if proxy_str:
                        a["proxy"] = proxy_str
            continue

        existing.append(
            {
                "id": acc_id,
                "email": email,
                "password": password,
                "recovery_email": recovery_email,
                "totp_secret": totp_secret,
                "proxy": proxy_str,
                "ip_info": "",
                "status": "pending",
                "current_step": 0,
                "step_message": "新导入账号（待执行）",
                "expiry_date": "-",
                "last_screenshot": "",
            }
        )
        existing_emails.add(norm_email)
        added += 1

    cfg["accounts"] = existing
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [IMPORT] ✅ 成功导入/更新 {added} 个账号（当前共 {len(existing)} 个账号）")
    return {"ok": True, "added": added, "total": len(existing)}


@app.post("/api/accounts/delete")
async def delete_account(req: AccountActionReq):
    if req.account_id in ACTIVE_TASKS:
        raise HTTPException(status_code=400, detail="该账号正在运行中，请先停止任务")
    cfg = load_config()
    cfg["accounts"] = [a for a in cfg.get("accounts", []) if a["id"] != req.account_id]
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [ACCOUNT] 🗑️ 已移除账号 {req.account_id}")
    return {"ok": True}


@app.post("/api/accounts/reset")
async def reset_account(req: AccountActionReq):
    cfg = load_config()
    for a in cfg.get("accounts", []):
        if a["id"] == req.account_id:
            a["status"] = "pending"
            a["current_step"] = 0
            a["step_message"] = "已重置状态（待执行）"
            break
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [ACCOUNT] 🔄 已重置账号 {req.account_id} 状态为待执行")
    return {"ok": True}


async def _run_account_worker(account_id: str, start_step: int = 1, port: int = 9320):
    import importlib
    import automation_engine
    importlib.reload(automation_engine)
    cfg = load_config()
    target_acc = None
    for a in cfg.get("accounts", []):
        if a["id"] == account_id:
            target_acc = a
            break
    if not target_acc:
        return

    def on_state_patch(patch: Dict[str, Any]):
        c = load_config()
        for acc_item in c.get("accounts", []):
            if acc_item["id"] == account_id:
                acc_item.update(patch)
                break
        save_config(c)

    automator = automation_engine.GoogleUltraAutomator(
        account=target_acc,
        card=cfg.get("card", {}),
        target_region=cfg.get("target_region", "US"),
        target_plan=cfg.get("target_plan", "pro_2year_zero"),
        auto_cancel=cfg.get("auto_cancel", True),
        proxy_template=cfg.get("proxy_template", ""),
        port=port,
        on_log=append_log,
        on_state=on_state_patch,
    )
    ACTIVE_TASKS[account_id] = automator
    try:
        await automator.run_pipeline(start_step=start_step)
    finally:
        ACTIVE_TASKS.pop(account_id, None)


@app.post("/api/run/single")
async def run_single(req: RunSingleReq):
    if req.account_id in ACTIVE_TASKS:
        raise HTTPException(status_code=400, detail="该账号已经在运行中")
    port = 9320 + (len(ACTIVE_TASKS) % 10)
    asyncio.create_task(_run_account_worker(req.account_id, start_step=req.start_step, port=port))
    return {"ok": True}


@app.post("/api/run/batch")
async def run_batch(req: RunBatchReq):
    global BATCH_RUNNING, STOP_BATCH
    if BATCH_RUNNING:
        raise HTTPException(status_code=400, detail="批量队列已经在运行中")

    cfg = load_config()
    if req.account_ids:
        queue_ids = req.account_ids
    else:
        queue_ids = [a["id"] for a in cfg.get("accounts", []) if a.get("status") != "completed"]

    if not queue_ids:
        raise HTTPException(status_code=400, detail="没有待执行的账号（已完成的账号如需重跑请先点重置）")

    async def _batch_runner():
        global BATCH_RUNNING, STOP_BATCH
        BATCH_RUNNING = True
        STOP_BATCH = False
        append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] 🚀 开始批量一条龙执行队列: {queue_ids} (起始步骤: Step {req.start_step})")
        try:
            for idx, aid in enumerate(queue_ids):
                if STOP_BATCH:
                    append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] ⏹ 批量队列已按用户要求停止")
                    break
                await _run_account_worker(aid, start_step=req.start_step, port=9330 + (idx % 5))
                await asyncio.sleep(2)
        finally:
            BATCH_RUNNING = False
            append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] 🏁 批量执行队列已结束")

    asyncio.create_task(_batch_runner())
    return {"ok": True, "queued": queue_ids}


@app.post("/api/stop")
async def stop_all(req: Optional[AccountActionReq] = None):
    global STOP_BATCH
    STOP_BATCH = True
    if req and req.account_id and req.account_id in ACTIVE_TASKS:
        ACTIVE_TASKS[req.account_id].stop_requested = True
        append_log(f"[{time.strftime('%H:%M:%S')}] [STOP] ⏹ 正在停止账号 {req.account_id}...")
    else:
        for aid, task in list(ACTIVE_TASKS.items()):
            task.stop_requested = True
        append_log(f"[{time.strftime('%H:%M:%S')}] [STOP] ⏹ 已发送全局停止指令")
    return {"ok": True}


MANUAL_BRIDGES: Dict[str, LocalHttpProxyAuthBridge] = {}


def _launch_on_interactive_desktop(args: List[str]):
    """
    Launch a GUI process (e.g. Chrome) explicitly onto the user's interactive desktop (winsta0\\default)
    even if the backend server was started inside an isolated/background desktop session (e.g. exebox).
    """
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class STARTUPINFOW(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD),
                    ("lpReserved", wt.LPWSTR),
                    ("lpDesktop", wt.LPWSTR),
                    ("lpTitle", wt.LPWSTR),
                    ("dwX", wt.DWORD),
                    ("dwY", wt.DWORD),
                    ("dwXSize", wt.DWORD),
                    ("dwYSize", wt.DWORD),
                    ("dwXCountChars", wt.DWORD),
                    ("dwYCountChars", wt.DWORD),
                    ("dwFillAttribute", wt.DWORD),
                    ("dwFlags", wt.DWORD),
                    ("wShowWindow", wt.WORD),
                    ("cbReserved2", wt.WORD),
                    ("lpReserved2", ctypes.POINTER(wt.BYTE)),
                    ("hStdInput", wt.HANDLE),
                    ("hStdOutput", wt.HANDLE),
                    ("hStdError", wt.HANDLE),
                ]

            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", wt.HANDLE),
                    ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD),
                    ("dwThreadId", wt.DWORD),
                ]

            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = "winsta0\\default"
            si.dwFlags = 1  # STARTF_USESHOWWINDOW
            si.wShowWindow = 3  # SW_MAXIMIZE
            pi = PROCESS_INFORMATION()
            cmd_str = subprocess.list2cmdline(args)
            cmd_buf = ctypes.create_unicode_buffer(cmd_str)
            ok = ctypes.windll.kernel32.CreateProcessW(
                None,
                cmd_buf,
                None,
                None,
                False,
                0x00000010,  # CREATE_NEW_CONSOLE
                None,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if ok:
                ctypes.windll.kernel32.CloseHandle(pi.hThread)
                ctypes.windll.kernel32.CloseHandle(pi.hProcess)
                return
        except Exception:
            pass
    subprocess.Popen(args)


@app.post("/api/open_browser")
async def open_browser_manual(req: AccountActionReq):
    cfg = load_config()
    target_acc = next((a for a in cfg.get("accounts", []) if a["id"] == req.account_id), None)
    if not target_acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    user_data_dir = get_profile_dir(target_acc["id"], target_acc["email"])
    target_region = cfg.get("target_region", "US")
    proxy_template = cfg.get("proxy_template", "")
    resolved_proxy = resolve_account_proxy(target_acc, target_region, proxy_template)
    parsed_proxy = parse_proxy_string(resolved_proxy)

    chrome_args = [
        CHROME_PATH,
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--enforce-webrtc-ip-permission-check",
        "--start-maximized",
    ]

    if parsed_proxy:
        if parsed_proxy.get("username"):
            if req.account_id in MANUAL_BRIDGES:
                try:
                    await MANUAL_BRIDGES[req.account_id].stop()
                except Exception:
                    pass
            acc_idx = sum(ord(c) for c in req.account_id) % 50
            bridge_port = 9600 + acc_idx
            bridge = LocalHttpProxyAuthBridge(bridge_port, parsed_proxy)
            await bridge.start()
            MANUAL_BRIDGES[req.account_id] = bridge
            chrome_args.append(f"--proxy-server=http://127.0.0.1:{bridge_port}")
        else:
            p_url = f"{parsed_proxy['scheme']}://{parsed_proxy['host']}:{parsed_proxy['port']}"
            chrome_args.append(f"--proxy-server={p_url}")

    chrome_args.extend(
        [
            "https://one.google.com/plans?hl=zh-CN",
            "https://play.google.com/store/account/subscriptions?hl=zh-CN",
            "https://play.google.com/store/account/orderhistory?hl=zh-CN",
        ]
    )
    _launch_on_interactive_desktop(chrome_args)
    append_log(
        f"[{time.strftime('%H:%M:%S')}] [BROWSER] 🌐 已为您手动打开 [{target_acc['id']}] ({target_acc['email']}) 的专属 Chrome 窗口"
    )
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
