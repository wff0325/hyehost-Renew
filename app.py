#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
# 续期成功提示 (toast): "Free hosting renewed for another 30 days."
RENEW_SUCCESS_RE = re.compile(r'renewed\s+(for another|successfully)', re.IGNORECASE)
# "Renewal due: 17/11/2026" / "Due 17/11/2026" (DD/MM/YYYY)
DUE_DATE_RE = re.compile(r'(?:Renewal due|Due)[\s:]*(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE)

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

def send_telegram_notification(status, old_due, new_due, current_ip="未知"):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram 未配置，跳过通知")
        return False
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    else:
        masked = EMAIL[:2] + '****'
    text = (
        f"HYEHOST 续期通知\n\n"
        f"{status}\n"
        f"账号: {masked}\n"
        f"续期前到期：{old_due}\n"
        f"续期后到期：{new_due}\n"
        f"续期使用IP: {current_ip}\n"
        f"续期时间：{now}"
    )
    url = f"http://qcjwtru.serv00.net:28344/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text},
                             timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("Telegram 通知发送成功")
            return True
        log(f"Telegram 通知失败: {resp.text}")
    except Exception as e:
        log(f"Telegram 通知异常: {e}")
    return False

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

# ==================== 服务发现 ====================
def get_service_url(page):
    """自动从 /hosting 页面发现服务详情页 URL"""
    try:
        if HOSTING_URL not in page.url:
            page.goto(HOSTING_URL, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)
        links = page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
        )
        for href in links:
            if not href:
                continue
            m = re.match(rf'^{re.escape(BASE_URL)}/hosting/([A-Za-z0-9_-]{{6,}})$', href)
            if m and m.group(1).lower() not in ('order',):
                url = f"{BASE_URL}/hosting/{m.group(1)}"
                log(f"发现服务: {url}")
                return url
            m2 = re.match(r'^/hosting/([A-Za-z0-9_-]{6,})$', href)
            if m2 and m2.group(1).lower() not in ('order',):
                url = f"{BASE_URL}/hosting/{m2.group(1)}"
                log(f"发现服务: {url}")
                return url
        log("未在 /hosting 页面找到服务链接")
    except Exception as e:
        log(f"获取服务链接异常: {e}")
    return None

# ==================== 到期时间 ====================
def get_due_date(page, service_url):
    """读取服务页的到期时间 (Renewal due: DD/MM/YYYY)"""
    try:
        if page.url != service_url:
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)
        body_text = page.locator("body").inner_text()
        m = DUE_DATE_RE.search(body_text)
        if m:
            due = m.group(1).strip()
            log(f"当前到期时间: {due}")
            return due
        log("未匹配到到期时间")
    except Exception as e:
        log(f"获取到期时间异常: {e}")
    return "未知"

# ==================== 续期 ====================
def renew_service(page, service_url):
    """执行续期，返回 True(成功) / False(失败) / 'NOT_TIME'(未到续期时间)"""
    try:
        log("进入续期流程...")
        if page.url != service_url:
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)

        renew_btn = page.locator('button:has-text("Renew free plan")')
        if renew_btn.count() == 0:
            renew_btn = page.locator('button:has-text("Renew")')
        if renew_btn.count() == 0:
            log("未找到续期按钮")
            page.screenshot(path="renew_no_button.png")
            return "NOT_TIME"

        btn = renew_btn.first
        try:
            if btn.is_disabled():
                log("续期按钮不可用（可能 30 天内已续期过）")
                return "NOT_TIME"
        except Exception:
            pass
        btn.scroll_into_view_if_needed()
        log("点击续期按钮...")
        btn.click(timeout=10000)

        # 等待结果：成功提示 / 错误提示
        start = time.time()
        last_text = ""
        while time.time() - start < 45:
            try:
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
                text = page.locator("body").inner_text()
                last_text = text
                if RENEW_SUCCESS_RE.search(text):
                    log("续期成功: " + RENEW_SUCCESS_RE.search(text).group(0))
                    time.sleep(2)
                    return True
                m = re.search(r'(only renew[^.\n]*|already renewed[^.\n]*)', text, re.IGNORECASE)
                if m:
                    log(f"未到续期时间: {m.group(1)}")
                    page.screenshot(path="renew_not_allowed.png")
                    return "NOT_TIME"
            except Exception:
                pass
            time.sleep(1)
        log(f"未检测到续期结果，页面文本末尾 300 字: {last_text[-300:]}")
        page.screenshot(path="renew_no_result.png")
        return False
    except Exception as e:
        log(f"续期异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False

# ==================== 主流程 ====================
def main():
    log(f"凭证检测: EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not EMAIL or not PASSWORD:
        log("缺少登录凭证，退出")
        sys.exit(1)

    service_url = SERVICE_URL
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

            if not service_url:
                service_url = get_service_url(page)
                if not service_url:
                    log("无法获取服务地址，退出")
                    sys.exit(1)

            old_due = get_due_date(page, service_url)
            log(f"续期前到期时间: {old_due}")

            renew_result = renew_service(page, service_url)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("未到续期时间，目前无法续期")
                status = "未到续期时间"
            elif renew_result is False:
                log("续期失败")
                status = "续期失败"
            else:
                # 续期后面板的到期时间可能有后台同步延迟，多次轮询读取
                new_due = old_due
                for i in range(6):
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        handle_cloudflare(page)
                        time.sleep(3)
                    except Exception:
                        pass
                    new_due = get_due_date(page, service_url)
                    if new_due != old_due:
                        break
                    if i < 5:
                        log("到期时间尚未刷新，5 秒后重试...")
                        time.sleep(5)
                log(f"续期后到期时间: {new_due}")
                status = "续期成功"

            send_telegram_notification(status, old_due, new_due, current_ip)

            if renew_result is False:
                sys.exit(1)
            sys.exit(0)
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
