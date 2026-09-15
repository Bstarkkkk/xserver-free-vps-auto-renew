import asyncio
import os
import sys
import socket
import logging
import aiohttp
import re
from urllib.parse import urlparse
from browserforge.fingerprints import Screen
from camoufox.async_api import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

RENEWAL_CONFIRM_TIMEOUT_MS = int(os.getenv('XSERVER_RENEWAL_CONFIRM_TIMEOUT_MS', '120000'))
RENEWAL_SUCCESS_URL = 'https://secure.xserver.ne.jp/xapanel/xvps/index'
SUCCESS_TEXT_HINTS = (
    '\u5229\u7528\u671f\u9650\u306e\u66f4\u65b0\u624b\u7d9a\u304d\u304c\u5b8c\u4e86\u3057\u307e\u3057\u305f',
    '\u66f4\u65b0\u304c\u5b8c\u4e86\u3057\u307e\u3057\u305f',
    '\u5951\u7d04\u66f4\u65b0\u304c\u5b8c\u4e86',
)
EXPIRY_LABEL = '\u66f4\u65b0\u5f8c\u306e\u5229\u7528\u671f\u9650'


async def page_body_text(page, timeout=5000):
    try:
        return await page.locator('body').inner_text(timeout=timeout)
    except Exception:
        return ''


def extract_expected_expiry(text):
    match = re.search(
        re.escape(EXPIRY_LABEL) + r'[^0-9]*(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        text or '',
    )
    return match.group(1).replace('/', '-') if match else ''


async def wait_for_renewal_confirmation(page, expected_expiry):
    logging.info('Waiting for XServer to confirm the renewal in the current session...')
    deadline = asyncio.get_running_loop().time() + RENEWAL_CONFIRM_TIMEOUT_MS / 1000
    last_url = page.url
    last_body = ''
    while asyncio.get_running_loop().time() < deadline:
        last_url = page.url
        if last_url.startswith(RENEWAL_SUCCESS_URL):
            last_body = await page_body_text(page)
            normalized_body = last_body.replace('/', '-')
            modal_confirmed = any(hint in last_body for hint in SUCCESS_TEXT_HINTS)
            expiry_confirmed = bool(expected_expiry and expected_expiry in normalized_body)
            if modal_confirmed or expiry_confirmed:
                await page.screenshot(path='renewal_confirmed.png', full_page=True)
                logging.info(f'Renewal confirmation URL: {last_url}')
                logging.info(f'Renewal confirmation expiry: {expected_expiry or "not captured"}')
                logging.info('Final renewal confirmed successfully!')
                return
        await asyncio.sleep(2)

    await capture_debug_context(page, 'renewal_confirmation_failed')
    excerpt = ' '.join(last_body.split())[:500]
    raise RuntimeError(
        'Renewal submission was clicked but could not be confirmed before timeout. '
        f'url={last_url} expected_expiry={expected_expiry or "unknown"} body={excerpt}'
    )


async def capture_debug_context(page, tag='debug'):
    info = {'tag': tag, 'url': '', 'title': '', 'body_excerpt': ''}
    try:
        info['url'] = page.url
    except Exception:
        pass
    try:
        info['title'] = await page.title()
    except Exception:
        pass
    try:
        body_text = await page.locator('body').inner_text(timeout=3000)
        body_text = ' '.join(body_text.split())
        info['body_excerpt'] = body_text[:1200]
    except Exception:
        pass
    try:
        await page.screenshot(path=f'{tag}.png', full_page=True)
    except Exception:
        pass
    logging.info(f"DEBUG_CONTEXT[{tag}]: url={info['url']}")
    logging.info(f"DEBUG_CONTEXT[{tag}]: title={info['title']}")
    logging.info(f"DEBUG_CONTEXT[{tag}]: body_excerpt={info['body_excerpt']}")
    return info

async def dismiss_campaign_modal(page):
    """Close/remove XServer free-user campaign modal if it blocks clicks."""
    try:
        modal = page.locator('#campaignModalForFreeUsers, .campaignModalForFreeUsers, .modal.isOpen').first
        if await modal.count() and await modal.is_visible(timeout=1000):
            logging.info('Dismissing campaign modal...')
            # Try normal close buttons first, then force-remove as fallback.
            for selector in [
                '#campaignModalForFreeUsers .modalClose',
                '#campaignModalForFreeUsers [class*=close]',
                '#campaignModalForFreeUsers button',
                '.modal.isOpen [class*=close]',
            ]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        await loc.click(timeout=2000, force=True, no_wait_after=True)
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    pass
            if await modal.is_visible(timeout=500):
                await page.evaluate("""
                    () => {
                        document.querySelectorAll('#campaignModalForFreeUsers, .campaignModalForFreeUsers, .modal.isOpen').forEach(el => el.remove());
                        document.querySelectorAll('.modalOverlay, .modal-backdrop, .overlay').forEach(el => el.remove());
                        document.body.style.overflow = 'auto';
                    }
                """)
                await page.wait_for_timeout(500)
    except Exception as e:
        logging.warning(f'Campaign modal dismiss skipped: {e}')

def get_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def build_pproxy_upstream(proxy_server: str) -> str:
    parsed = urlparse(proxy_server)
    scheme = (parsed.scheme or '').lower()
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise ValueError(f'Invalid proxy URL: {proxy_server}')
    if scheme != 'socks5':
        raise ValueError(f'Only socks5 can use pproxy bridge, got: {scheme}')
    if parsed.username or parsed.password:
        user = parsed.username or ''
        password = parsed.password or ''
        return f'socks5://{host}:{port}#{user}:{password}'
    return f'socks5://{host}:{port}'


async def start_pproxy_bridge(proxy_server: str):
    port = get_free_local_port()
    listen = f'http://127.0.0.1:{port}'
    upstream = build_pproxy_upstream(proxy_server)
    log_path = f'/tmp/pproxy-{port}.log'
    logging.info(f'Starting local proxy bridge: {listen} -> {upstream}')
    logf = open(log_path, 'ab')
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'pproxy', '-l', listen, '-r', upstream,
        stdout=logf,
        stderr=logf,
    )

    last_error = None
    for _ in range(30):
        if proc.returncode is not None:
            raise RuntimeError(f'pproxy exited early with code {proc.returncode}. log={log_path}')
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.close()
            await writer.wait_closed()
            logging.info(f'Local proxy bridge is ready at {listen}')
            return {'server': listen, 'proc': proc, 'logf': logf, 'log_path': log_path}
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.2)

    proc.terminate()
    try:
        await proc.wait()
    except Exception:
        pass
    logf.close()
    raise RuntimeError(f'pproxy bridge did not become ready: {last_error}. log={log_path}')


async def stop_pproxy_bridge(bridge) -> None:
    if not bridge:
        return
    proc = bridge.get('proc')
    logf = bridge.get('logf')
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
    if logf:
        try:
            logf.close()
        except Exception:
            pass


async def main():
    email = os.getenv('EMAIL', '')
    password = os.getenv('PASSWORD', '')
    proxy_server = os.getenv('PROXY_SERVER')
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'

    options = {
        'headless': False,
        'humanize': True,
        'geoip': True,
        'os': 'macos',
        'screen': Screen(max_width=1280, max_height=720),
        'window': (1280, 720),
        'locale': 'ja-JP',
        'disable_coop': True,
        'i_know_what_im_doing': True,
        'config': {'forceScopeAccess': True},
        'main_world_eval': True,
        'addons': [os.path.abspath(get_addon_path())],
        'firefox_user_prefs': {},
    }

    bridge = None
    if proxy_server:
        parsed = urlparse(proxy_server)
        scheme = (parsed.scheme or '').lower()
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            raise ValueError(f'Invalid proxy URL: {proxy_server}')

        if scheme == 'socks5':
            bridge = await start_pproxy_bridge(proxy_server)
            options['proxy'] = {'server': bridge['server']}
            logging.info('Configured SOCKS5 proxy via local pproxy bridge.')
        elif scheme in ('http', 'https'):
            proxy_config = {
                'server': f"{scheme}://{host}:{port}"
            }
            if parsed.username:
                proxy_config['username'] = parsed.username
            if parsed.password:
                proxy_config['password'] = parsed.password
            options['proxy'] = proxy_config
            logging.info('Configured HTTP(S) proxy via Playwright proxy settings.')
        else:
            raise ValueError(f'Unsupported proxy scheme: {scheme}')
    
    logging.info('Launching Camoufox in Python...')
    async with AsyncCamoufox(**options) as browser:
        context = await browser.new_context()
        page = await context.new_page()
        framework = FrameworkType.CAMOUFOX

        try:
            logging.info('Navigating to login...')
            # Use domcontentloaded to avoid getting stuck on tracking pixels
            await page.goto('https://secure.xserver.ne.jp/xapanel/login/xvps/', wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_selector('#memberid', timeout=30000)
            
            await page.locator('#memberid').fill(email)
            await page.locator('#user_password').fill(password)
            
            logging.info('Logging in...')
            login_btn = page.locator('text="ログインする"')
            try:
                await login_btn.click(no_wait_after=True)
            except Exception:
                await capture_debug_context(page, 'login_click_failed')
                raise
            
            # Manually wait for the dashboard to appear instead of waiting for networkidle
            logging.info('Waiting for dashboard to load...')
            try:
                await page.wait_for_selector('a[href^="/xapanel/xvps/server/detail?id="]', timeout=30000)
            except Exception:
                await capture_debug_context(page, 'dashboard_wait_failed')
                raise

            await dismiss_campaign_modal(page)

            logging.info('Navigating server details...')
            await page.locator('a[href^="/xapanel/xvps/server/detail?id="]').first.click(no_wait_after=True)
            
            logging.info('Waiting for server detail page...')
            await page.wait_for_selector('text="更新する"', timeout=30000)
            await page.locator('text="更新する"').click()

            logging.info('Proceeding to renewal selection...')
            await page.locator('text="引き続き無料VPSの利用を継続する"').click(no_wait_after=True)
            
            logging.info('Waiting for renewal page or status...')
            
            # Wait for either the captcha image OR the suspension notice section
            # This will raise TimeoutError if neither appears within 30s (correct behavior)
            await page.wait_for_selector('img[src^="data:"], .newApp__suspended', timeout=30000)
            
            # If the suspension notice is visible, skip renewal gracefully
            if await page.locator('.newApp__suspended').is_visible():
                logging.info('SKIP: Renewal is not yet available (detected .newApp__suspended).')
                logging.info('XServer: "利用期限の1日前から更新手続きが可能です。"')
                await page.screenshot(path='skip_renewal.png', full_page=True)
                await capture_debug_context(page, 'skip_renewal')
                return

            logging.info('Retrieving captcha...')
            body = await page.eval_on_selector('img[src^="data:"]', 'img => img.src')
            
            # Solve custom image captcha
            async with aiohttp.ClientSession() as session:
                async with session.post('https://ocr.858880.xyz/', data=body) as resp:
                    code = await resp.text()
            
            logging.info(f'Resolved captcha code: {code}')
            
            input_loc = page.locator('[placeholder="上の画像の数字を入力"]')
            await input_loc.focus()
            await input_loc.press_sequentially(code, delay=100)
            
            try:
                # Use playwright-captcha library to handle the Turnstile challenge
                async with ClickSolver(framework=framework, page=page) as solver:
                    await solver.solve_captcha(captcha_container=page, captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE)
                logging.info('Turnstile interaction finished.')
            except Exception as e:
                # Some solvers might throw errors even if the click was successful.
                # We catch and log them as warnings to allow the script to proceed.
                logging.warning(f'Turnstile solve loop exited: {e}')

            await page.wait_for_selector('text="無料VPSの利用を継続する"', timeout=30000)
            await page.screenshot(path='before_click.png', full_page=True)
            expected_expiry = extract_expected_expiry(await page_body_text(page))
            
            button = page.locator('text="無料VPSの利用を継続する"')
            is_disabled = False
            try:
                is_disabled = await button.is_disabled()
            except Exception:
                pass
                
            if is_disabled:
                err_msg = 'Final button is DISABLED! Renewal failed or Turnstile verification was unsuccessful.'
                logging.error(err_msg)
                if not debug_mode:
                    raise Exception(err_msg)
            else:
                if debug_mode:
                    logging.info('DEBUG MODE: Final button is ENABLED and ready to click. Skipping click to preserve daily limit.')
                else:
                    logging.info('Executing final renewal submission...')
                    await button.click(timeout=15000, no_wait_after=True)
                    await wait_for_renewal_confirmation(page, expected_expiry)
            
            logging.info('Done!')
        
        except Exception as e:
            try:
                await capture_debug_context(page, 'fatal_error')
            except Exception:
                pass
            logging.error(f'Script Error: {e}')
            sys.exit(1)
        finally:
            await asyncio.sleep(2)
            await context.close()
            await stop_pproxy_bridge(bridge)

if __name__ == '__main__':
    asyncio.run(main())
