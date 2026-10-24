#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HYEHOST 共享主机/Bot Hosting(免费计划)自动续期脚本
参考项目: https://github.com/wff0325/yincangyun (HidenCloud 续期)

支持多服务类型自动续期: 登录后自动发现 Shared Hosting(/hosting) 和
Bot Hosting(/bots) 下的所有服务, 逐个读取到期时间并续期。
新增服务类型只需在 SERVICE_TYPES 列表加一项(列表页路径/链接正则/续期按钮文本)。

用法:
    本地运行(凭证放 .env 文件，已在 .gitignore 中，不会提交):
        在 .env 中写入 EMAIL/PASSWORD，然后 python app.py
    GitHub Actions:
        仓库 Settings -> Secrets and variables -> Actions 添加 Secrets:
        EMAIL(必填) PASSWORD(必填) TG_BOT_TOKEN(可选) TG_CHAT_ID(可选) NODE_LINK(可选代理)

配置项(环境变量):
    EMAIL          登录邮箱            (必填; .env 或 GitHub Secrets)
    PASSWORD       登录密码            (必填; .env 或 GitHub Secrets)
    SERVICE_URL    服务详情页完整URL    (可选, 留空则自动发现所有类型的所有服务)
    TG_BOT_TOKEN   Telegram Bot Token  (可选, 通知)
    TG_CHAT_ID     Telegram Chat ID    (可选, 通知)
    NODE_LINK      代理节点分享链接      (可选; 由 workflow 的 sing-box 步骤消费,
                   支持 vless:// vmess:// trojan:// tuic:// anytls:// hysteria2:// socks5:// socks://)
    IS_PROXY       是否使用代理         (默认 false; workflow 代理步骤自动写入)
    PROXY_SERVER   代理地址            (默认 http://127.0.0.1:1081)
    BROWSER_CHANNEL 浏览器通道          (默认自动: msedge -> chrome -> 内置chromium)
    SECURITY_TIMEOUT 安全验证总等待秒数 (默认 240)
"""

import os, re, sys, time, json, random, shutil

# ==================== 本地 .env 加载(不提交仓库) ====================
def _load_local_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except Exception:
        pass

_load_local_env()

# ==================== CONFIG (环境变量优先) ====================
EMAIL           = os.environ.get('EMAIL') or ""
PASSWORD        = os.environ.get('PASSWORD') or ""
SERVICE_URL     = os.environ.get('SERVICE_URL') or ""          # 例: https://panel.hyehost.net/hosting/xxxxxxxx
TG_BOT_TOKEN    = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID      = os.environ.get('TG_CHAT_ID') or ""
IS_PROXY        = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER    = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
BROWSER_CHANNEL = os.environ.get('BROWSER_CHANNEL') or ""     # 空=自动探测
SECURITY_TIMEOUT = int(os.environ.get('SECURITY_TIMEOUT') or "240")

REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

BASE_URL  = "https://panel.hyehost.net"
LOGIN_URL = f"{BASE_URL}/login"
HOSTING_URL = f"{BASE_URL}/hosting"

try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

import requests

# ==================== 常量 ====================
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'
SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required',
                        'checking your browser', 'one more step')
DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
# 续期成功提示 (toast): "Free hosting renewed for another 30 days." / "Free bot renewed for another 30 days."
RENEW_SUCCESS_RE = re.compile(r'renewed\s+(for another|successfully)', re.IGNORECASE)
# 到期时间匹配(兼容多格式):
#   "Renewal due: 17/11/2026" (DD/MM/YYYY, 共享主机)
#   "Current renewal date: 2026年11月23日" (中文, Bot Hosting)
#   "2026-11-23" (兜底)
DUE_DATE_RES = [
    re.compile(r'(?:Renewal due|Due)\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE),
    re.compile(r'(?:renewal date|expiry|due)\s*:?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', re.IGNORECASE),
    re.compile(r'(\d{4}-\d{2}-\d{2})'),
]

def _parse_due(text):
    for i, rx in enumerate(DUE_DATE_RES):
        m = rx.search(text)
        if m:
            if i == 1:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            return m.group(1).strip()
    return None

def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ==================== Cloudflare 过盾 ====================
def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(summary, current_ip="未知"):
    """发送 Telegram 通知(汇总多服务续期结果)"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram 未配置，跳过通知")
        return
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    else:
        masked = EMAIL[:2] + '****'
    text = (
        f"HYEHOST 续期通知\n\n"
        f"{summary}\n"
        f"账号: {masked}\n"
        f"续期使用IP: {current_ip}\n"
        f"续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text},
                             timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("Telegram 通知发送成功")
            return True
        log(f"Telegram 通知失败: {resp.text}")
    except Exception as e:
        log(f"Telegram 通知异常: {e}")

def _get_turnstile_token(page):
    """读取 Turnstile token，未通过时返回 None"""
    try:
        return page.evaluate("""() => {
            if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                let t = null;
                try { t = window.turnstile.getResponse(); } catch(e) {}
                if (t && t.length > 30) return t;
                for (let i = 0; i < 5; i++) {
                    try { t = window.turnstile.getResponse(String(i)); } catch(e) { t = null; }
                    if (t && t.length > 30) return t;
                }
            }
            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 30) return inp.value;
            return null;
        }""")
    except Exception:
        return None

def _find_visible_cf_frames(page):
    """返回可见且有实际尺寸的 CF 挑战/Turnstile iframe"""
    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = handle.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return result

def _cf_checkbox_visible(page):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False

def _click_cf_checkbox(page, frame_el=None):
    """点击 CF/Turnstile 复选框：先试框架内元素，失败退化为坐标点击"""
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False

def _is_security_check_page(page):
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate("""() => {
            const t = document.body ? document.body.innerText.slice(0,5000).toLowerCase() : '';
            return t.includes('verify you are human') || t.includes('checking your browser')
                || t.includes('security verification');
        }""")
    except Exception:
        return True

def _has_interstitial_iframe(page):
    """是否存在整页挑战 iframe（排除 /turnstile/ 组件 iframe）"""
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (handle.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False

def handle_cloudflare(page, timeout=None):
    """整页 Cloudflare 挑战处理"""
    if timeout is None:
        timeout = SECURITY_TIMEOUT
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)
    if not challenge_active():
        return True
    log("检测到 Cloudflare 安全验证...")
    time.sleep(3)
    start = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start < timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2:
                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("点击 Turnstile 验证...")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("验证超时")
    return False

def solve_turnstile(page, timeout=90):
    """处理页面上的 Turnstile 组件（如登录表单内嵌），通过后返回 True"""
    if _get_turnstile_token(page):
        return True
    frames = _find_visible_cf_frames(page)
    if not frames:
        time.sleep(2)
        if _get_turnstile_token(page):
            return True
        if not _find_visible_cf_frames(page):
            return True  # 无 Turnstile
    log("处理 Turnstile 人机验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    while time.time() - start < timeout:
        if _get_turnstile_token(page):
            log("Turnstile 验证通过")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            break
        if time.time() - last_click > 5:
            log(f"点击 Turnstile 复选框 (第 {clicks + 1} 次)...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(2, 4))
                continue
        time.sleep(1)
    tok = _get_turnstile_token(page)
    if tok:
        log("Turnstile 验证通过")
    else:
        log("Turnstile 未确认通过")
    return bool(tok)


# ==================== 浏览器 ====================
def _detect_channel():
    if BROWSER_CHANNEL:
        return BROWSER_CHANNEL
    if sys.platform == 'win32':
        ch_paths = {
            'chrome': [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
            'msedge': [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                       r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        }
        for ch, paths in ch_paths.items():
            if any(os.path.exists(p) for p in paths):
                return ch
        return None
    # Linux(GitHub Actions 已通过 patchright install chrome 装好真实 Chrome)
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    if shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
        return "msedge"
    return "chrome"

def open_browser(p):
    """启动浏览器，返回 (browser, page)。优先 patchright + 真实浏览器通道。"""
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    channel = _detect_channel()
    if USING_PATCHRIGHT and channel:
        try:
            browser = p.chromium.launch(channel=channel, headless=False,
                                        args=['--disable-infobars'], proxy=proxy_arg)
            ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, proxy=proxy_arg)
            log(f"启动反检测内核浏览器 (patchright + {channel})")
            return browser, ctx.new_page()
        except Exception as e:
            log(f"patchright+{channel} 启动失败: {e}")
    log("使用 playwright 内置 chromium (过 CF 能力较弱，建议安装 patchright)")
    browser = p.chromium.launch(headless=False,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars'])
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900},
                              user_agent=DEFAULT_UA, proxy=proxy_arg)
    page = ctx.new_page()
    page.add_init_script("try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}")
    return browser, page

# ==================== 登录 ====================
def login(page):
    """账号密码登录 hyehost 面板，成功返回 True"""
    if not EMAIL or not PASSWORD:
        log("缺少登录凭证 (EMAIL/PASSWORD)")
        return False
    try:
        log("打开登录页...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        time.sleep(2)

        log("输入账号密码...")
        user_input = page.locator('input#email, input[type="email"], input[name="email"]').first
        pwd_input = page.locator('input#password, input[type="password"], input[name="password"]').first
        user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(EMAIL, timeout=10000)
        pwd_input.fill(PASSWORD, timeout=10000)
        time.sleep(0.5)

        # 登录表单内嵌 Turnstile，提交前必须通过
        solve_turnstile(page, timeout=90)

        log("点击 Continue 登录...")
        try:
            page.click('button[type="submit"]:has-text("Continue"), button:has-text("Continue")',
                       timeout=8000)
        except Exception:
            page.locator('button[type="submit"]').first.click(timeout=10000)
        time.sleep(4)
        handle_cloudflare(page)

        nav_start = time.time()
        navigated = False
        while time.time() - nav_start < 120:
            try:
                if '/login' not in page.url and not _is_security_check_page(page):
                    navigated = True
                    break
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
            except Exception:
                pass
            time.sleep(1)
        if not navigated:
            # 可能第一次提交未触发（token 过期），重试一次
            log("未跳转，重新过验证后重试提交...")
            solve_turnstile(page, timeout=60)
            try:
                page.locator('button:has-text("Continue")').first.click(timeout=8000)
                for _ in range(20):
                    if '/login' not in page.url:
                        break
                    time.sleep(1)
                navigated = '/login' not in page.url
            except Exception:
                pass
        if not navigated:
            log("登录失败，请检查账号密码或验证流程")
            page.screenshot(path="login_fail.png")
            return False
        log(f"登录成功: {page.url}")
        return True
    except Exception as e:
        log(f"登录异常: {e}")
        try:
            page.screenshot(path="login_fail.png")
        except Exception:
            pass
        return False

def _is_logged_in(page):
    try:
        url = page.url or ''
        if BASE_URL not in url or '/login' in url:
            return False
        return not _is_security_check_page(page)
    except Exception:
        return False

# ==================== 服务类型定义(新增服务类型只需在此加一项) ====================
SERVICE_TYPES = [
    {
        "name": "Shared Hosting",
        "list_path": "/hosting",
        "link_re": r"^/hosting/([A-Za-z0-9_-]{6,})$",
        "renew_texts": ["Renew free plan"],
    },
    {
        "name": "Bot Hosting",
        "list_path": "/bots",
        "link_re": r"^/bots/([A-Za-z0-9_-]{6,})$",
        "renew_texts": ["Renew for 30 days"],
    },
]

def discover_services(page):
    """遍历 SERVICE_TYPES 列表页, 返回 [{"type": 类型名, "url": 服务页URL}]"""
    services = []
    seen = set()
    for st in SERVICE_TYPES:
        try:
            page.goto(BASE_URL + st["list_path"], wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)
            links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
            )
            found = 0
            for href in links:
                if not href:
                    continue
                if re.match(st["link_re"], href) and href not in seen:
                    seen.add(href)
                    url = href if href.startswith("http") else BASE_URL + href
                    services.append({"type": st["name"], "url": url})
                    found += 1
            log(f"{st['name']}: 发现 {found} 个服务")
        except Exception as e:
            log(f"获取 {st['name']} 服务列表异常: {e}")
    return services

# ==================== 到期时间 ====================
def get_due_date(page):
    """读取当前服务页的到期时间(自动兼容 DD/MM/YYYY、YYYY年M月D日、YYYY-MM-DD)"""
    try:
        text = page.locator("body").inner_text()
        due = _parse_due(text)
        if due:
            return due
    except Exception as e:
        log(f"读取到期时间异常: {e}")
    return "未知"

def _read_due_with_retry(page, url, old_due, retries=6):
    """续期后轮询读取新到期时间(hyehost 面板有后台同步延迟, 读不到就返回旧值)"""
    new_due = old_due
    for i in range(retries):
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(3)
        except Exception:
            pass
        new_due = get_due_date(page)
        if new_due != old_due:
            break
        if i < retries - 1:
            log("到期时间尚未刷新，5 秒后重试...")
            time.sleep(5)
    return new_due

# ==================== 续期 ====================
def _find_renew_button(page, svc):
    """按服务类型精确匹配续期按钮文本, 最后才用通用 'Renew' 兜底"""
    texts = []
    for st in SERVICE_TYPES:
        if st["name"] == svc.get("type"):
            texts = list(st.get("renew_texts", []))
    texts.append("Renew")
    for t in texts:
        loc = page.locator(f'button:has-text("{t}")')
        if loc.count() > 0:
            return loc.first
    return None

def renew_service(page, svc):
    """执行单个服务续期, 返回 (结果, 旧到期, 新到期)
    结果: True成功 / False失败 / 'NOT_TIME'未到续期时间"""
    url = svc["url"]
    stype = svc["type"]
    tag = stype.replace(" ", "_")
    try:
        log(f"[{stype}] 进入续期流程...")
        if page.url != url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)

        old_due = get_due_date(page)
        log(f"[{stype}] 续期前到期: {old_due}")

        btn = _find_renew_button(page, svc)
        if btn is None:
            log(f"[{stype}] 未找到续期按钮")
            page.screenshot(path=f"renew_no_button_{tag}.png")
            return False, old_due, old_due
        try:
            if btn.is_disabled():
                log(f"[{stype}] 续期按钮不可用（可能 30 天内已续期过）")
                return "NOT_TIME", old_due, old_due
        except Exception:
            pass
        btn.scroll_into_view_if_needed()
        log(f"[{stype}] 点击续期按钮...")
        btn.click(timeout=10000)

        # 等待结果: 成功提示 / 未到续期时间提示
        start = time.time()
        last_text = ""
        while time.time() - start < 45:
            try:
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
                text = page.locator("body").inner_text()
                last_text = text
                if RENEW_SUCCESS_RE.search(text):
                    log(f"[{stype}] 续期成功: " + RENEW_SUCCESS_RE.search(text).group(0))
                    time.sleep(2)
                    new_due = _read_due_with_retry(page, url, old_due)
                    return True, old_due, new_due
                m = re.search(r'(only renew[^.\n]*|already renewed[^.\n]*)', text, re.IGNORECASE)
                if m:
                    log(f"[{stype}] 未到续期时间: {m.group(1)}")
                    page.screenshot(path=f"renew_not_allowed_{tag}.png")
                    return "NOT_TIME", old_due, old_due
            except Exception:
                pass
            time.sleep(1)
        log(f"[{stype}] 未检测到续期结果，页面文本末尾 300 字: {last_text[-300:]}")
        page.screenshot(path=f"renew_no_result_{tag}.png")
        return False, old_due, old_due
    except Exception as e:
        log(f"[{stype}] 续期异常: {e}")
        try:
            page.screenshot(path=f"renew_error_{tag}.png")
        except Exception:
            pass
        return False, "未知", "未知"

# ==================== 主流程 ====================
def main():
    log(f"凭证检测: EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not EMAIL or not PASSWORD:
        log("缺少登录凭证，退出")
        sys.exit(1)

    with sync_playwright() as p:
        browser = None
        try:
            if IS_PROXY:
                log(f"代理已启用: {PROXY_SERVER}")
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"当前出口IP: {current_ip}")

            browser, page = open_browser(p)
            page.set_default_timeout(15000)

            if not login(page):
                sys.exit(1)

            # 发现所有服务(Shared Hosting / Bot Hosting / ...), SERVICE_URL 可固定单个
            if SERVICE_URL:
                stype = "Shared Hosting" if "/hosting/" in SERVICE_URL else (
                    "Bot Hosting" if "/bots/" in SERVICE_URL else "Service")
                services = [{"type": stype, "url": SERVICE_URL}]
                log(f"使用固定服务: {SERVICE_URL}")
            else:
                services = discover_services(page)
            if not services:
                log("未发现任何服务，退出")
                sys.exit(1)

            results = []
            for svc in services:
                result, old_due, new_due = renew_service(page, svc)
                results.append((svc, result, old_due, new_due))

            # 汇总
            lines = []
            has_fail = False
            for svc, result, old_due, new_due in results:
                if result == "NOT_TIME":
                    s = "未到续期时间"
                elif result is True:
                    s = "续期成功"
                else:
                    s = "续期失败"
                    has_fail = True
                line = f"• {svc['type']}: {s}（{old_due} -> {new_due}）"
                lines.append(line)
                log(line)
            send_telegram_notification("\n".join(lines), current_ip)

            sys.exit(1 if has_fail else 0)
        except Exception as e:
            log(f"运行出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
