#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib import parse, request
from uuid import uuid4

BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TG_CHAT_ID', '').strip()
PROJECT_DIR = Path(__file__).resolve().parent
WORKDIR = str(PROJECT_DIR)
LOG_DIR = PROJECT_DIR / 'logs'
WORKSPACE = PROJECT_DIR
UPLOAD_ENDPOINT = os.getenv('XSERVER_IMAGE_UPLOAD_URL', '').strip()
UPLOAD_REFERER = os.getenv('XSERVER_IMAGE_UPLOAD_REFERER', '').strip()
SKIP_MARKER = 'SKIP: Renewal is not yet available'
SUCCESS_MARKER = 'Final renewal confirmed successfully!'

IMAGE_CANDIDATES = [
    WORKSPACE / 'renewal_confirmed.png',
    WORKSPACE / 'final_result.png',
    WORKSPACE / 'SUCCESS_RENEWAL_FINAL.png',
    WORKSPACE / 'SUCCESS_XVFB.png',
    WORKSPACE / 'xserver_final.png',
    WORKSPACE / 'xserver_final_push.png',
    WORKSPACE / 'FAIL_FINAL.png',
    WORKSPACE / 'STILL_STUCK.png',
    WORKSPACE / 'no_renew_redirect.png',
    WORKSPACE / 'skip_renewal.png',
    WORKSPACE / 'before_click.png',
]


def http_post(url: str, data: bytes = None, headers: dict | None = None, timeout: int = 30):
    req = request.Request(url, data=data, headers=headers or {})
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return body.decode('utf-8', errors='replace'), resp.headers


def tg_send(text: str):
    data = parse.urlencode({'chat_id': CHAT_ID, 'text': text}).encode()
    return http_post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', data=data)[0]


def tg_send_photo(photo_path: str, caption: str):
    boundary = f'----OpenClawTG{uuid4().hex}'
    p = Path(photo_path)
    mime = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    photo_bytes = p.read_bytes()

    parts = []

    def add_field(name: str, value: str):
        parts.append(f'--{boundary}\r\n'.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode('utf-8'))
        parts.append(b'\r\n')

    add_field('chat_id', CHAT_ID)
    add_field('caption', caption)

    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(f'Content-Disposition: form-data; name="photo"; filename="{p.name}"\r\n'.encode())
    parts.append(f'Content-Type: {mime}\r\n\r\n'.encode())
    parts.append(photo_bytes)
    parts.append(b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())

    body = b''.join(parts)
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
    return http_post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto', data=body, headers=headers, timeout=60)[0]


def upload_image(photo_path: str):
    p = Path(photo_path)
    boundary = f'----OpenClawUpload{uuid4().hex}'
    mime = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    photo_bytes = p.read_bytes()

    parts = []

    def add_field(name: str, value: str):
        parts.append(f'--{boundary}\r\n'.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode('utf-8'))
        parts.append(b'\r\n')

    add_field('strategy_id', os.getenv('XSERVER_IMAGE_STRATEGY_ID', ''))
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'.encode())
    parts.append(f'Content-Type: {mime}\r\n\r\n'.encode())
    parts.append(photo_bytes)
    parts.append(b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(parts)

    headers = {
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Accept': 'application/json, text/plain, */*',
        'Referer': UPLOAD_REFERER,
        'Origin': UPLOAD_REFERER,
        'User-Agent': 'Mozilla/5.0 OpenClaw XServer notifier',
    }

    raw, _ = http_post(UPLOAD_ENDPOINT, data=body, headers=headers, timeout=90)
    payload = json.loads(raw)
    candidates = [
        payload.get('data', {}).get('url') if isinstance(payload.get('data'), dict) else None,
        payload.get('data', {}).get('links', {}).get('url') if isinstance(payload.get('data'), dict) and isinstance(payload.get('data', {}).get('links'), dict) else None,
        payload.get('url'),
    ]
    image_url = next((x for x in candidates if isinstance(x, str) and x.strip()), None)
    if not image_url:
        raise RuntimeError(f'图床返回里没找到图片链接: {raw[:500]}')
    return image_url, payload


def infer_expiry(text: str):
    patterns = [
        r'利用期限[^\n]*?(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        r'有効期限[^\n]*?(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        r'期限[^\n]*?(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return '未知'


def pick_screenshot(start_ts: float | None = None):
    candidates = []
    for path in IMAGE_CANDIDATES:
        if path.exists() and path.is_file():
            stat = path.stat()
            if start_ts is None or stat.st_mtime >= start_ts - 5:
                candidates.append((stat.st_mtime, str(path)))

    if not candidates:
        for ext in ('*.png', '*.jpg', '*.jpeg', '*.webp'):
            for path in WORKSPACE.glob(ext):
                stat = path.stat()
                if start_ts is None or stat.st_mtime >= start_ts - 5:
                    candidates.append((stat.st_mtime, str(path)))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def extract_debug_context(out: str) -> dict:
    debug = {'url': '', 'title': '', 'body_excerpt': ''}
    for line in out.splitlines():
        if 'DEBUG_CONTEXT' not in line:
            continue
        if 'url=' in line:
            debug['url'] = line.split('url=', 1)[1].strip()
        elif 'title=' in line:
            debug['title'] = line.split('title=', 1)[1].strip()
        elif 'body_excerpt=' in line:
            debug['body_excerpt'] = line.split('body_excerpt=', 1)[1].strip()
    return debug


def summarize_failure_reason(out: str, run_ok: bool) -> str:
    if run_ok:
        return '无'
    lower = out.lower()
    if 'browser does not support socks5 proxy authentication' in lower or 'pproxy exited early' in lower or 'ns_error_connection_refused' in lower:
        return 's5代理失效'
    if 'could not be confirmed before timeout' in lower:
        return '续期未确认生效'
    if 'timeout' in lower or 'timed out' in lower:
        if 'navigated to "https://secure.xserver.ne.jp/xapanel/login/xvps/"' in out:
            return '密码错误'
        return '网络超时'
    if 'proxy' in lower and ('refused' in lower or 'unreachable' in lower or 'failed' in lower):
        return 's5代理失效'
    if 'page.wait_for_selector' in lower and 'login/xvps/' in lower:
        return '密码错误'
    if 'locator.click: timeout' in lower and 'ログインする' in out:
        return '网络超时'
    if '运行成功，但未识别到最终续期结果' in out:
        return '未确认续期结果'
    if 'success confirmation failed' in lower:
        return '成功后复查失败'
    return '未知'


def build_message(label: str, run_ok: bool, need_sign: bool, sign_status: str, expiry: str, failure_reason: str = '无', debug_context: dict | None = None, screenshot_path: str | None = None, image_url: str | None = None):
    title = '🖥 XServer 续期结果'
    status_line = '✅ 运行成功' if run_ok else '❌ 运行失败'
    need_line = '🟡 今天需要续期' if need_sign else '⚪️ 今天无需续期'
    lines = [title, f'账号：{label}', status_line, need_line, f'结果：{sign_status}']
    if not run_ok:
        lines.append(f'原因：{failure_reason}')
    return '\n'.join(lines)


def run_xserver_once(cmd, run_env, start_ts: float, attempt: int):
    proc = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=900, env=run_env)
    out = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
    process_ok = proc.returncode == 0
    skipped = SKIP_MARKER in out
    submitted = SUCCESS_MARKER in out
    recognized = skipped or submitted
    run_ok = process_ok and recognized
    need_sign = not skipped
    if skipped:
        sign_status = '今天无需续期'
    elif submitted:
        sign_status = '今天已确认续期成功'
    elif process_ok:
        sign_status = '运行成功，但未识别到最终续期结果'
    else:
        sign_status = '运行失败'
    return {
        'attempt': attempt,
        'out': out,
        'run_ok': run_ok,
        'process_ok': process_ok,
        'skipped': skipped,
        'submitted': submitted,
        'recognized': recognized,
        'need_sign': need_sign,
        'sign_status': sign_status,
        'expiry': infer_expiry(out),
        'screenshot_path': pick_screenshot(start_ts),
    }


def finalize_attempts(attempts: list[dict]) -> dict:
    final = dict(attempts[-1])
    submitted_any = any(a.get('submitted') for a in attempts)

    if submitted_any and final.get('skipped'):
        final['run_ok'] = True
        final['need_sign'] = True
        final['sign_status'] = '今天已执行续期（复查已生效）'
        submitted_shot = next((a.get('screenshot_path') for a in reversed(attempts) if a.get('submitted') and a.get('screenshot_path')), None)
        if submitted_shot:
            final['screenshot_path'] = submitted_shot
    elif submitted_any and final.get('submitted') and final.get('run_ok') and len(attempts) == 1:
        final['run_ok'] = True
        final['need_sign'] = True
    elif submitted_any and final.get('submitted') and final.get('run_ok'):
        final['run_ok'] = False
        final['need_sign'] = True
        final['sign_status'] = '复查后仍可提交，未确认生效'
        final['out'] = final.get('out', '') + '\nERROR: renewal still appears submittable after confirmation'
    elif submitted_any and not final.get('run_ok'):
        final['need_sign'] = True
        final['sign_status'] = '成功后复查失败'
        final['out'] = final.get('out', '') + '\nERROR: success confirmation failed'

    return final


def main():
    email = os.getenv('EMAIL', '').strip()
    password = os.getenv('PASSWORD', '').strip()
    label = os.getenv('ACCOUNT_LABEL', email or 'unknown')
    if not email or not password:
        raise SystemExit('EMAIL/PASSWORD missing')

    start_ts = datetime.now().timestamp()
    safe_label = re.sub(r'[^a-zA-Z0-9_.-]+', '_', label)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f'xserver_{safe_label}.log'

    run_env = os.environ.copy()
    run_env['EMAIL'] = email
    run_env['PASSWORD'] = password
    run_env['DISPLAY'] = ':99'
    cmd = ['bash', '-c', '. .venv/bin/activate; python main.py']

    attempts = [run_xserver_once(cmd, run_env, start_ts, 1)]

    with log_path.open('a', encoding='utf-8') as f:
        f.write(f"\n[{datetime.now().isoformat(sep=' ', timespec='seconds')}]\n")
        for attempt_result in attempts:
            f.write(f"--- attempt {attempt_result['attempt']} ---\n")
            f.write(attempt_result['out'])
            if not attempt_result['out'].endswith('\n'):
                f.write('\n')

    final_result = finalize_attempts(attempts)
    run_ok = final_result['run_ok']
    need_sign = final_result['need_sign']
    sign_status = final_result['sign_status']
    expiry = final_result['expiry']
    screenshot_path = final_result['screenshot_path']
    retry_attempted = len(attempts) > 1
    image_url = None
    failure_reason = summarize_failure_reason(final_result['out'], run_ok)
    debug_context = extract_debug_context(final_result['out'])

    msg = build_message(label, run_ok, need_sign, sign_status, expiry, failure_reason=failure_reason, debug_context=debug_context, screenshot_path=screenshot_path)
    if retry_attempted:
        msg += '\n🔁 首次失败，已自动重试一次'

    sent_via = 'text'
    send_errors = []
    if BOT_TOKEN and CHAT_ID:
        if screenshot_path:
            try:
                tg_send_photo(screenshot_path, msg)
                sent_via = 'telegram-photo'
            except Exception as e:
                send_errors.append(f'TG发图失败: {e}')
                try:
                    if not UPLOAD_ENDPOINT or not UPLOAD_REFERER:
                        raise RuntimeError('image upload not configured')
                    image_url, _payload = upload_image(screenshot_path)
                    tg_send(msg + f'\n🔗 图片链接：{image_url}')
                    sent_via = 'imagebed-link'
                except Exception as upload_err:
                    send_errors.append(f'图床上传失败: {upload_err}')
                    tg_send(msg)
                    sent_via = 'text-after-failure'
        else:
            tg_send(msg)

    result = {
        'label': label,
        'run_ok': run_ok,
        'need_sign': need_sign,
        'sign_status': sign_status,
        'expiry': expiry,
        'failure_reason': failure_reason,
        'debug_context': debug_context,
        'screenshot_path': screenshot_path,
        'image_url': image_url,
        'sent_via': sent_via,
        'retry_attempted': retry_attempted,
        'attempts': len(attempts),
        'confirm_after_success': False,
        'submitted_any': any(a.get('submitted') for a in attempts),
        'final_attempt': final_result.get('attempt'),
        'errors': send_errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not run_ok:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
