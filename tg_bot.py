#!/usr/bin/env python3
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from urllib import parse, request

BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '').strip()
ADMIN_TG_ID = os.getenv('ADMIN_TG_ID', '').strip()
DB_PATH = Path(os.getenv('BOT_DB_PATH', './bot_users.db'))
POLL_TIMEOUT = int(os.getenv('TG_POLL_TIMEOUT', '30'))
WORKDIR = os.getenv('XSERVER_WORKDIR', os.getcwd())
DEFAULT_SIGN_TIME_SH = os.getenv('DEFAULT_SIGN_TIME_SH', '08:00')
# Renew window is based on XServer 12:00 JST = 11:00 SH.
RENEW_WINDOW_START_SH = os.getenv('RENEW_WINDOW_START_SH', '11:10')
RENEW_WINDOW_END_SH = os.getenv('RENEW_WINDOW_END_SH', '22:40')
RENEW_DEADLINE_SH = os.getenv('RENEW_DEADLINE_SH', '22:45')
RENEW_RETRY_MINUTES = int(os.getenv('RENEW_RETRY_MINUTES', '30'))
RENEW_RETRY_DELAY_LATE_MINUTES = int(os.getenv('RENEW_RETRY_DELAY_LATE_MINUTES', '5'))
RENEW_MAX_RETRIES_PER_DAY = int(os.getenv('RENEW_MAX_RETRIES_PER_DAY', '20'))
RENEW_STALE_QUEUE_MINUTES = int(os.getenv('RENEW_STALE_QUEUE_MINUTES', '60'))
SUMMARY_TIME_SH = os.getenv('SUMMARY_TIME_SH', '22:50')

TZ_SH = timezone(timedelta(hours=8))
TZ_JP = timezone(timedelta(hours=9))
EMAIL_RE = re.compile(r'([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})')

USER_QUEUES: dict[str, Queue] = {}
USER_WORKERS: dict[str, threading.Thread] = {}
USER_RUNNING: set[str] = set()
QUEUE_LOCK = threading.Lock()
PENDING_ACTIONS: dict[str, dict] = {}


def now_sh():
    return datetime.now(TZ_SH)


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                tg_id TEXT PRIMARY KEY,
                username TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id TEXT NOT NULL,
                account_alias TEXT,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                proxy_server TEXT,
                sign_time_sh TEXT DEFAULT '08:00',
                enabled INTEGER DEFAULT 1,
                last_sign_date_sh TEXT,
                renew_enabled INTEGER DEFAULT 1,
                next_renew_at_sh TEXT,
                schedule_date_sh TEXT,
                last_enqueue_key TEXT,
                last_success_date_sh TEXT,
                last_attempt_at_sh TEXT,
                retry_count_today INTEGER DEFAULT 0,
                last_result TEXT,
                last_result_at_sh TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(tg_id, email)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_summaries (
                tg_id TEXT NOT NULL,
                summary_date_sh TEXT NOT NULL,
                sent_at_sh TEXT,
                PRIMARY KEY (tg_id, summary_date_sh)
            )
        ''')
        ensure_account_columns(conn)


def ensure_account_columns(conn):
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(accounts)').fetchall()}
    columns = {
        'renew_enabled': 'INTEGER DEFAULT 1',
        'next_renew_at_sh': 'TEXT',
        'schedule_date_sh': 'TEXT',
        'last_enqueue_key': 'TEXT',
        'last_success_date_sh': 'TEXT',
        'last_attempt_at_sh': 'TEXT',
        'retry_count_today': 'INTEGER DEFAULT 0',
        'last_result': 'TEXT',
        'last_result_at_sh': 'TEXT',
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE accounts ADD COLUMN {name} {ddl}')


def tg_api(method: str, payload: dict):
    data = parse.urlencode(payload).encode()
    req = request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}', data=data)
    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def send_msg(chat_id: str, text: str, reply_markup: dict | None = None):
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return tg_api('sendMessage', payload)


def answer_callback(callback_id: str, text: str = ''):
    payload = {'callback_query_id': callback_id}
    if text:
        payload['text'] = text
    return tg_api('answerCallbackQuery', payload)


def edit_message(chat_id: str, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return tg_api('editMessageText', payload)


def delete_message(chat_id: str, message_id: int):
    return tg_api('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})


def try_delete_user_message(msg):
    try:
        chat_id = str(msg['chat']['id'])
        message_id = int(msg['message_id'])
        delete_message(chat_id, message_id)
    except Exception:
        pass


def is_hhmm(s: str):
    if len(s) != 5 or s[2] != ':':
        return False
    h, m = s.split(':')
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def is_valid_proxy(proxy: str) -> bool:
    p = proxy.strip().lower()
    return p.startswith('socks5://')


def mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return '未设置'
    p = proxy.strip()
    if not p:
        return '未设置'
    if '@' not in p:
        return p
    left, right = p.rsplit('@', 1)
    if '://' in left:
        scheme = left.split('://', 1)[0]
        return f'{scheme}://******@{right}'
    return '******'


def mask_email(email: str | None) -> str:
    if not email or '@' not in email:
        return email or ''
    local, domain = email.split('@', 1)
    if len(local) <= 2:
        masked_local = local[:1] + '******'
    elif len(local) <= 5:
        masked_local = local[:2] + '******'
    else:
        masked_local = local[:2] + '******' + local[-3:]
    return f'{masked_local}@{domain}'


def redact_emails(text: str) -> str:
    return EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text or '')


def account_requires_proxy(account) -> bool:
    return str(account['tg_id']) != str(ADMIN_TG_ID)


def can_account_sign(account) -> tuple[bool, str]:
    if account_requires_proxy(account) and not (account['proxy_server'] or '').strip():
        return False, '必须先添加 socks5 代理后才可以进行签到。'
    return True, ''


def is_admin_user(tg_id: str) -> bool:
    return str(tg_id) == ADMIN_TG_ID


def set_user(tg_id: str, username: str):
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        r = conn.execute('SELECT tg_id FROM users WHERE tg_id=?', (tg_id,)).fetchone()
        if r:
            conn.execute('UPDATE users SET username=?, updated_at=? WHERE tg_id=?', (username, now, tg_id))
        else:
            default_enabled = 1
            conn.execute('INSERT INTO users (tg_id, username, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)', (tg_id, username, default_enabled, now, now))


def is_user_enabled(tg_id: str) -> bool:
    if is_admin_user(tg_id):
        return True
    with db_conn() as conn:
        row = conn.execute('SELECT enabled FROM users WHERE tg_id=?', (tg_id,)).fetchone()
    return bool(row['enabled']) if row else True


def list_users():
    with db_conn() as conn:
        return conn.execute('SELECT tg_id, username, enabled, created_at, updated_at FROM users ORDER BY updated_at DESC, created_at DESC').fetchall()


def get_user_row(tg_id: str):
    with db_conn() as conn:
        return conn.execute('SELECT tg_id, username, enabled, created_at, updated_at FROM users WHERE tg_id=?', (tg_id,)).fetchone()


def set_user_enabled(tg_id: str, enabled: int):
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute('UPDATE users SET enabled=?, updated_at=? WHERE tg_id=?', (enabled, now, tg_id))


def upsert_account(tg_id: str, email: str, password: str, proxy_server: str | None, alias: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        row = conn.execute('SELECT id FROM accounts WHERE tg_id=? AND email=?', (tg_id, email)).fetchone()
        if row:
            conn.execute(
                'UPDATE accounts SET password=?, proxy_server=?, account_alias=COALESCE(?, account_alias), updated_at=? WHERE id=?',
                (password, proxy_server, alias, now, row['id'])
            )
            return row['id']
        cur = conn.execute(
            'INSERT INTO accounts (tg_id, account_alias, email, password, proxy_server, sign_time_sh, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)',
            (tg_id, alias, email, password, proxy_server, DEFAULT_SIGN_TIME_SH, now, now)
        )
        return cur.lastrowid


def list_accounts(tg_id: str):
    with db_conn() as conn:
        return conn.execute('SELECT * FROM accounts WHERE tg_id=? ORDER BY email', (tg_id,)).fetchall()


def count_accounts_for_user(tg_id: str) -> int:
    with db_conn() as conn:
        row = conn.execute('SELECT COUNT(*) AS c FROM accounts WHERE tg_id=?', (tg_id,)).fetchone()
    return int(row['c']) if row else 0


def count_missing_proxy_accounts_for_user(tg_id: str) -> int:
    with db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE tg_id=? AND (proxy_server IS NULL OR trim(proxy_server)='')", (tg_id,)).fetchone()
    return int(row['c']) if row else 0


def get_account(account_id: int):
    with db_conn() as conn:
        return conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()


def get_account_by_email(tg_id: str, email: str):
    with db_conn() as conn:
        return conn.execute('SELECT * FROM accounts WHERE tg_id=? AND lower(email)=lower(?)', (tg_id, email.strip())).fetchone()


def list_tg_ids_with_accounts():
    with db_conn() as conn:
        return [
            row['tg_id']
            for row in conn.execute('SELECT DISTINCT tg_id FROM accounts ORDER BY tg_id').fetchall()
        ]


def update_account(account_id: int, field: str, value):
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(f'UPDATE accounts SET {field}=?, updated_at=? WHERE id=?', (value, now, account_id))


def delete_account(account_id: int):
    with db_conn() as conn:
        conn.execute('DELETE FROM accounts WHERE id=?', (account_id,))



def update_account_fields(account_id: int, **fields):
    if not fields:
        return
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    names = list(fields.keys())
    sql = ', '.join([f'{name}=?' for name in names])
    values = [fields[name] for name in names]
    values.append(account_id)
    with db_conn() as conn:
        conn.execute(f'UPDATE accounts SET {sql} WHERE id=?', values)


def parse_hhmm_minutes(value: str, fallback: str) -> int:
    raw = (value or fallback).strip()
    if not is_hhmm(raw):
        raw = fallback
    h, m = raw.split(':')
    return int(h) * 60 + int(m)


def sh_dt_for_minutes(date_obj, minute_of_day: int):
    return datetime(date_obj.year, date_obj.month, date_obj.day, minute_of_day // 60, minute_of_day % 60, tzinfo=TZ_SH)


def iso_sh(dt: datetime) -> str:
    return dt.astimezone(TZ_SH).replace(microsecond=0).isoformat()


def parse_sh_iso(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_SH)
        return dt.astimezone(TZ_SH)
    except Exception:
        return None


def stable_daily_seed(today: str, rows) -> int:
    raw = today + '|' + '|'.join([f'{r["id"]}:{r["email"]}' for r in rows])
    return int(hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16], 16)


def get_auto_accounts():
    with db_conn() as conn:
        return conn.execute('''
            SELECT * FROM accounts
            WHERE enabled=1 AND COALESCE(renew_enabled, 1)=1
            ORDER BY lower(email), id
        ''').fetchall()


def account_success_date(account) -> str:
    return account['last_success_date_sh'] or account['last_sign_date_sh'] or ''


def set_account_success(account_id: int):
    today = now_sh().date().isoformat()
    update_account_fields(
        account_id,
        last_success_date_sh=today,
        last_sign_date_sh=today,
        retry_count_today=0,
        last_result='success',
        last_result_at_sh=iso_sh(now_sh()),
    )


def schedule_account_retry(account_id: int, raw_out: str = ''):
    acc = get_account(account_id)
    if not acc:
        return None
    now = now_sh()
    today = now.date().isoformat()
    if account_success_date(acc) == today:
        return None

    retry_count = int(acc['retry_count_today'] or 0) + 1
    end_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_WINDOW_END_SH, '22:40'))
    deadline_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_DEADLINE_SH, '22:45'))

    if retry_count > RENEW_MAX_RETRIES_PER_DAY:
        next_dt = deadline_dt if now < deadline_dt else now + timedelta(minutes=1)
        result = 'retry_limit_urgent'
    else:
        next_dt = now + timedelta(minutes=RENEW_RETRY_MINUTES)
        result = 'retry_scheduled'
        if next_dt > end_dt:
            next_dt = now + timedelta(minutes=RENEW_RETRY_DELAY_LATE_MINUTES)
            result = 'urgent_retry'
        if next_dt > deadline_dt and now < deadline_dt:
            next_dt = now + timedelta(minutes=1)
            result = 'deadline_retry'

    update_account_fields(
        account_id,
        next_renew_at_sh=iso_sh(next_dt),
        schedule_date_sh=today,
        last_enqueue_key=None,
        retry_count_today=retry_count,
        last_result=result,
        last_result_at_sh=iso_sh(now),
    )
    return next_dt


def generate_daily_random_schedule():
    now = now_sh()
    today = now.date().isoformat()
    rows = [r for r in get_auto_accounts() if account_success_date(r) != today]
    need = [r for r in rows if r['schedule_date_sh'] != today or not r['next_renew_at_sh']]
    if not need:
        return

    start_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_WINDOW_START_SH, '11:10'))
    end_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_WINDOW_END_SH, '22:40'))
    deadline_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_DEADLINE_SH, '22:45'))

    if now < start_dt:
        available_start = start_dt
        available_end = end_dt
    elif now <= end_dt:
        available_start = now + timedelta(minutes=5)
        available_end = end_dt
    elif now <= deadline_dt:
        available_start = now
        available_end = deadline_dt
    else:
        available_start = now
        available_end = now + timedelta(minutes=max(1, len(need)))

    total_seconds = max(0, int((available_end - available_start).total_seconds()))
    rng = random.Random(stable_daily_seed(today, need))
    ordered = sorted(need, key=lambda r: hashlib.sha256(f'{today}:{r["id"]}:{r["email"]}'.encode('utf-8')).hexdigest())
    n = len(ordered)
    planned_by_tg: dict[str, list[tuple[str, datetime]]] = {}

    for idx, acc in enumerate(ordered):
        if total_seconds <= 0:
            next_dt = now + timedelta(seconds=idx * 30)
        else:
            seg_start = total_seconds * idx // n
            seg_end = total_seconds * (idx + 1) // n
            offset = seg_start if seg_end <= seg_start else rng.randint(seg_start, max(seg_start, seg_end - 1))
            next_dt = (available_start + timedelta(seconds=offset)).replace(second=0, microsecond=0)
        update_account_fields(
            acc['id'],
            next_renew_at_sh=iso_sh(next_dt),
            schedule_date_sh=today,
            last_enqueue_key=None,
            retry_count_today=0,
            last_result='scheduled',
            last_result_at_sh=iso_sh(now),
        )
        planned_by_tg.setdefault(acc['tg_id'], []).append((mask_email(acc['email']), next_dt))

    for tg_id, items in planned_by_tg.items():
        items.sort(key=lambda item: item[1])
        lines = [
            '🎲 今日自动续期计划',
            '',
            f'日期：{today}（上海）',
            f'窗口：{RENEW_WINDOW_START_SH} - {RENEW_WINDOW_END_SH}（上海）',
            '',
        ]
        for email, next_dt in items:
            jp_dt = next_dt.astimezone(TZ_JP)
            lines.append(
                f'{next_dt.strftime("%H:%M")} SH / {jp_dt.strftime("%H:%M")} JP - {email}'
            )
        try:
            send_msg(tg_id, '\n'.join(lines))
        except Exception:
            pass


def enqueue_scheduled_account(acc, reason: str):
    today = now_sh().date().isoformat()
    retry_count = int(acc['retry_count_today'] or 0)
    enqueue_key = f'{acc["id"]}:{today}:{acc["next_renew_at_sh"]}:{retry_count}'
    now = now_sh()
    if acc['last_enqueue_key'] == enqueue_key:
        last_attempt = parse_sh_iso(acc['last_attempt_at_sh'])
        if last_attempt and now - last_attempt <= timedelta(minutes=RENEW_STALE_QUEUE_MINUTES):
            return None
    update_account_fields(
        acc['id'],
        last_enqueue_key=enqueue_key,
        last_attempt_at_sh=iso_sh(now),
        last_result='queued',
        last_result_at_sh=iso_sh(now),
    )
    return enqueue_job(acc['tg_id'], acc['id'], reason)


def run_sign_for_account(account):
    env = os.environ.copy()
    env['EMAIL'] = account['email']
    env['PASSWORD'] = account['password']
    env['TG_BOT_TOKEN'] = BOT_TOKEN
    env['TG_CHAT_ID'] = account['tg_id']
    env['ACCOUNT_LABEL'] = mask_email(account['email'])
    if account['proxy_server']:
        env['PROXY_SERVER'] = account['proxy_server']
    else:
        env.pop('PROXY_SERVER', None)
    proc = subprocess.run(['python3', 'run_xserver_notify.py'], cwd=WORKDIR, env=env, capture_output=True, text=True, timeout=1200)
    return proc.returncode == 0, (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')


def daily_summary_already_sent(tg_id: str, today: str) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            'SELECT 1 FROM daily_summaries WHERE tg_id=? AND summary_date_sh=?',
            (tg_id, today),
        ).fetchone()
    return bool(row)


def mark_daily_summary_sent(tg_id: str, today: str):
    with db_conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO daily_summaries (tg_id, summary_date_sh, sent_at_sh) VALUES (?, ?, ?)',
            (tg_id, today, iso_sh(now_sh())),
        )


def account_summary_status(acc, today: str, now: datetime) -> tuple[str, str]:
    display_email = mask_email(acc['email'])
    if acc['enabled'] != 1:
        return 'skipped', f'\u23f8 {display_email} - \u5df2\u7981\u7528'
    if account_success_date(acc) == today:
        return 'success', f'\u2705 {display_email} - \u6210\u529f'

    retry_count = int(acc['retry_count_today'] or 0)
    result = acc['last_result'] or ''
    next_dt = parse_sh_iso(acc['next_renew_at_sh'])

    if acc['schedule_date_sh'] != today:
        return 'pending', f'\u23f3 {display_email} - \u4eca\u65e5\u672a\u751f\u6210\u8ba1\u5212'
    if result == 'queued':
        return 'pending', f'\u23f3 {display_email} - \u5df2\u5165\u961f/\u53ef\u80fd\u6267\u884c\u4e2d'
    if next_dt and now < next_dt:
        retry_text = f'\uff0c\u5df2\u91cd\u8bd5 {retry_count} \u6b21' if retry_count else ''
        return 'pending', f'\u23f3 {display_email} - \u7b49\u5f85 {next_dt.strftime("%H:%M")}{retry_text}'
    if retry_count:
        return 'failed', f'\u274c {display_email} - \u672a\u6210\u529f\uff0c\u5df2\u91cd\u8bd5 {retry_count} \u6b21'
    result_text = result or '\u65e0\u7ed3\u679c'
    return 'failed', f'\u274c {display_email} - \u672a\u6210\u529f\uff08{result_text}\uff09'


def build_daily_summary_text(tg_id: str, today: str, now: datetime) -> str | None:
    accounts = list_accounts(tg_id)
    if not accounts:
        return None

    counts = {'success': 0, 'failed': 0, 'pending': 0, 'skipped': 0}
    lines = []
    for acc in accounts:
        key, line = account_summary_status(acc, today, now)
        counts[key] += 1
        lines.append(line)

    with QUEUE_LOCK:
        q_size = USER_QUEUES.get(tg_id).qsize() if tg_id in USER_QUEUES else 0
        running = tg_id in USER_RUNNING
    queue_line = f'\n\u961f\u5217\uff1a{"执行中" if running else "空闲"} / \u6392\u961f {q_size}'

    return '\n'.join([
        '\U0001f4cb \u4eca\u65e5\u7eed\u671f\u6c47\u603b',
        '',
        f'\u65e5\u671f\uff1a{today}\uff08\u4e0a\u6d77\uff09',
        f'\u8d26\u53f7\u6570\uff1a{len(accounts)}',
        f'\u6210\u529f\uff1a{counts["success"]}',
        f'\u5931\u8d25\uff1a{counts["failed"]}',
        f'\u5f85\u5904\u7406\uff1a{counts["pending"]}',
        f'\u8df3\u8fc7\uff1a{counts["skipped"]}',
        queue_line,
        '',
        *lines,
    ])


def send_daily_summaries_if_due():
    now = now_sh()
    today = now.date().isoformat()
    summary_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(SUMMARY_TIME_SH, '22:50'))
    if now < summary_dt:
        return

    for tg_id in list_tg_ids_with_accounts():
        if not is_user_enabled(tg_id):
            continue
        if daily_summary_already_sent(tg_id, today):
            continue
        text = build_daily_summary_text(tg_id, today, now)
        if not text:
            continue
        send_msg(tg_id, text)
        mark_daily_summary_sent(tg_id, today)


def main_menu_markup(has_accounts: bool, is_admin: bool = False):
    rows = [
        [{'text': '➕ 添加账号', 'callback_data': 'menu:add'}],
    ]
    if has_accounts:
        rows.extend([
            [{'text': '📂 我的账号', 'callback_data': 'menu:accounts'}, {'text': '📊 状态', 'callback_data': 'menu:status'}],
            [{'text': '▶️ 立即签到', 'callback_data': 'menu:sign_select'}],
        ])
    if is_admin:
        rows.append([{'text': '🛠 Admin', 'callback_data': 'menu:admin'}])
    return {'inline_keyboard': rows}


def admin_menu_markup():
    return {
        'inline_keyboard': [
            [{'text': '👥 用户管理', 'callback_data': 'admin:users'}],
            [{'text': '⬅️ 返回首页', 'callback_data': 'menu:home'}],
        ]
    }


def admin_users_markup(rows):
    buttons = []
    for row in rows[:50]:
        status = '✅' if row['enabled'] else '⛔️'
        name = row['username'] or '未命名用户'
        account_count = count_accounts_for_user(row['tg_id'])
        missing_proxy = count_missing_proxy_accounts_for_user(row['tg_id'])
        suffix = f' | {account_count}个账号'
        if missing_proxy:
            suffix += f' | 缺代理{missing_proxy}'
        buttons.append([{'text': f'{status} {name} ({row["tg_id"]}){suffix}', 'callback_data': f'admin:user:{row["tg_id"]}'}])
    buttons.append([{'text': '⬅️ 返回 Admin', 'callback_data': 'menu:admin'}])
    return {'inline_keyboard': buttons}


def admin_user_manage_markup(row):
    enabled = bool(row['enabled'])
    return {
        'inline_keyboard': [
            [{'text': '📂 查看账号列表', 'callback_data': f'admin:accounts:{row["tg_id"]}'}],
            [{'text': '⛔️ 禁用' if enabled else '✅ 启用', 'callback_data': f'admin:{"disable" if enabled else "enable"}:{row["tg_id"]}'}],
            [{'text': '⬅️ 返回用户列表', 'callback_data': 'admin:users'}],
        ]
    }


def account_list_markup(accounts, mode='view'):
    rows = []
    for acc in accounts[:30]:
        label = acc['email']
        if mode == 'sign':
            rows.append([{'text': f'▶️ {label}', 'callback_data': f'sign:{acc["id"]}'}])
        elif mode == 'manage':
            rows.append([{'text': f'⚙️ {label}', 'callback_data': f'account:{acc["id"]}'}])
        else:
            rows.append([{'text': label, 'callback_data': f'account:{acc["id"]}'}])
    rows.append([{'text': '⬅️ 返回首页', 'callback_data': 'menu:home'}])
    return {'inline_keyboard': rows}


def account_manage_markup(acc):
    enabled = '\u2705 \u5df2\u542f\u7528' if acc['enabled'] else '\u26d4\ufe0f \u5df2\u7981\u7528'
    toggle = 'disable' if acc['enabled'] else 'enable'
    toggle_text = '\u26d4\ufe0f \u7981\u7528' if acc['enabled'] else '\u2705 \u542f\u7528'
    rows = [
        [{'text': '\u25b6\ufe0f \u7acb\u5373\u7b7e\u5230', 'callback_data': f'sign:{acc["id"]}'}],
        [{'text': '\U0001f4ca \u72b6\u6001', 'callback_data': 'menu:status'}, {'text': '\U0001f310 \u8bbe\u7f6e\u4ee3\u7406', 'callback_data': f'setproxy_prompt:{acc["id"]}'}],
        [{'text': toggle_text, 'callback_data': f'{toggle}:{acc["id"]}'}, {'text': '\U0001f5d1 \u5220\u9664\u8d26\u53f7', 'callback_data': f'del_confirm:{acc["id"]}'}],
        [{'text': '\U0001f4c2 \u8fd4\u56de\u8d26\u53f7\u5217\u8868', 'callback_data': 'menu:accounts'}],
    ]
    return enabled, {'inline_keyboard': rows}


def account_summary(acc):
    status = '\u2705 \u5df2\u542f\u7528' if acc['enabled'] else '\u26d4\ufe0f \u5df2\u7981\u7528'
    proxy = mask_proxy(acc['proxy_server'])
    last = acc['last_success_date_sh'] or acc['last_sign_date_sh'] or '\u6682\u65e0'
    next_renew = acc['next_renew_at_sh'] or '\u7b49\u5f85\u751f\u6210'
    result = acc['last_result'] or '\u6682\u65e0'
    eligible = can_account_sign(acc)[1] if not can_account_sign(acc)[0] else '\u53ef\u7b7e\u5230'
    return (
        f'\U0001f9fe \u8d26\u53f7\u9762\u677f\n\n'
        f'\U0001f4e7 \u90ae\u7bb1\uff1a{acc["email"]}\n'
        f'\U0001f4cc \u72b6\u6001\uff1a{status}\n'
        f'\U0001f3b2 \u81ea\u52a8\u8ba1\u5212\uff1a{next_renew}\n'
        f'\U0001f9e6 \u4ee3\u7406\uff1a{proxy}\n'
        f'\U0001f4c5 \u6700\u8fd1\u6210\u529f\uff1a{last}\n'
        f'\U0001f4dd \u6700\u8fd1\u7ed3\u679c\uff1a{result}\n'
        f'\U0001f6a6 \u7b7e\u5230\u8d44\u683c\uff1a{eligible}'
    )


def queue_status_text(chat_id: str):
    rows = list_accounts(chat_id)
    with QUEUE_LOCK:
        q_size = USER_QUEUES.get(chat_id).qsize() if chat_id in USER_QUEUES else 0
        running = chat_id in USER_RUNNING
    return (
        f'📊 当前状态\n\n'
        f'账号数：{len(rows)}\n'
        f'队列长度：{q_size}\n'
        f'是否执行中：{running}\n'
        f'当前上海时间：{now_sh().strftime("%Y-%m-%d %H:%M:%S")}'
    )


def admin_text():
    users = list_users()
    enabled_count = sum(1 for u in users if u['enabled'])
    disabled_count = len(users) - enabled_count
    total_accounts = sum(count_accounts_for_user(u['tg_id']) for u in users)
    total_missing_proxy = sum(count_missing_proxy_accounts_for_user(u['tg_id']) for u in users)
    return (
        '🛠 Admin 面板\n\n'
        f'管理员 TG ID：{ADMIN_TG_ID}\n'
        f'用户总数：{len(users)}\n'
        f'可用用户：{enabled_count}\n'
        f'已禁用用户：{disabled_count}\n'
        f'总账号数：{total_accounts}\n'
        f'缺代理账号：{total_missing_proxy}\n\n'
        '你可以在这里管理谁能使用机器人。'
    )


def help_text():
    return (
        '🖥 XServer 自动续期\n\n'
        '⚠️ 使用前请先关闭 XServer 两步验证（2FA）\n'
        '🕒 所有时间均为上海时间（UTC+8）\n'
        '🧦 必须添加代理才可以进行签到\n'
        '✅ 当前仅支持 socks5 代理\n\n'
        '推荐直接点击下方按钮操作。\n'
        '如果你想直接发送文字，请使用：\n\n'
        '邮箱 密码 socks5代理\n\n'
        '格式示范：\n'
        'xxxxxxx@gmail.com your_password socks5://user:pass@1.2.3.4:1080'
    )


def enqueue_job(tg_id: str, account_id: int, reason: str):
    with QUEUE_LOCK:
        q = USER_QUEUES.get(tg_id)
        if q is None:
            q = Queue()
            USER_QUEUES[tg_id] = q
        if tg_id not in USER_WORKERS or not USER_WORKERS[tg_id].is_alive():
            t = threading.Thread(target=user_worker, args=(tg_id,), daemon=True)
            USER_WORKERS[tg_id] = t
            t.start()
        q.put((account_id, reason, now_sh().isoformat()))
        pos = q.qsize()
    return pos


def user_worker(tg_id: str):
    while True:
        q = USER_QUEUES[tg_id]
        account_id, reason, _created = q.get()
        with QUEUE_LOCK:
            USER_RUNNING.add(tg_id)
        try:
            account = get_account(account_id)
            if not account or account['enabled'] != 1:
                send_msg(tg_id, f'\u26a0\ufe0f \u8d26\u53f7\u5df2\u5220\u9664\u6216\u5df2\u7981\u7528\uff0c\u8df3\u8fc7\u6267\u884c')
                continue
            display_email = mask_email(account['email'])
            send_msg(
                tg_id,
                f'\U0001f680 \u5f00\u59cb\u6267\u884c\u7eed\u671f\n\n\u8d26\u53f7\uff1a{display_email}\n\u89e6\u53d1\u539f\u56e0\uff1a{reason}\n\u65f6\u95f4\uff1a{now_sh().strftime("%Y-%m-%d %H:%M:%S")}\uff08\u4e0a\u6d77\uff09'
            )
            allowed, block_reason = can_account_sign(account)
            if not allowed:
                send_msg(tg_id, f'\u26d4\ufe0f {display_email} \u6682\u4e0d\u80fd\u7eed\u671f\n\n\u539f\u56e0\uff1a{block_reason}')
                schedule_account_retry(account_id, block_reason)
                continue
            ok, raw_out = run_sign_for_account(account)
            if ok:
                set_account_success(account_id)
            else:
                tail = redact_emails(raw_out.strip())[-800:]
                next_dt = schedule_account_retry(account_id, raw_out)
                retry_text = f'\n\n\u5df2\u5b89\u6392\u91cd\u8bd5\uff1a{next_dt.strftime("%Y-%m-%d %H:%M:%S")}\uff08\u4e0a\u6d77\uff09' if next_dt else ''
                send_msg(tg_id, f'\u274c {display_email} \u7eed\u671f\u5931\u8d25{retry_text}\n\n\u8fd0\u884c\u8f93\u51fa\uff1a\n{tail}')
        finally:
            with QUEUE_LOCK:
                USER_RUNNING.discard(tg_id)
            q.task_done()


def parse_account_text(text: str):
    parts = text.split()
    if len(parts) < 3 or '@' not in parts[0]:
        return None
    email = parts[0].strip()
    password = parts[1].strip()
    proxy = parts[2].strip() if is_valid_proxy(parts[2]) else None
    if not proxy:
        return None
    return email, password, proxy, None


def prompt_add_account(chat_id: str):
    PENDING_ACTIONS[chat_id] = {'action': 'add_account'}
    send_msg(chat_id, '请直接发送：\n\n邮箱 密码 socks5代理\n\n说明：必须添加代理才可以进行签到。\n\n格式示范：\nxxxxxxx@gmail.com your_password socks5://user:pass@1.2.3.4:1080')


def prompt_settime(chat_id: str, account_id: int):
    acc = get_account(account_id)
    if not acc or acc['tg_id'] != chat_id:
        send_msg(chat_id, '账号不存在。')
        return
    PENDING_ACTIONS[chat_id] = {'action': 'set_time', 'account_id': account_id}
    send_msg(chat_id, f'请回复 {acc["email"]} 的新签到时间，格式如 08:00（上海时间）')


def prompt_setproxy(chat_id: str, account_id: int):
    acc = get_account(account_id)
    if not acc or acc['tg_id'] != chat_id:
        send_msg(chat_id, '账号不存在。')
        return
    PENDING_ACTIONS[chat_id] = {'action': 'set_proxy', 'account_id': account_id}
    send_msg(chat_id, f'请回复 {acc["email"]} 的 socks5 代理。\n如需清空，回复 none\n\n格式示范：\nsocks5://user:pass@1.2.3.4:1080')


def send_home(chat_id: str):
    accounts = list_accounts(chat_id)
    text = help_text()
    if accounts:
        text += f'\n\n你当前已添加 {len(accounts)} 个账号。'
    send_msg(chat_id, text, reply_markup=main_menu_markup(bool(accounts), chat_id == ADMIN_TG_ID))


def send_accounts(chat_id: str):
    accounts = list_accounts(chat_id)
    if not accounts:
        send_msg(chat_id, '你还没有添加账号。', reply_markup=main_menu_markup(False))
        return
    lines = ['📂 账号列表', '', '说明：必须添加代理才可以进行签到。']
    for acc in accounts:
        status = '✅' if acc['enabled'] else '⛔️'
        proxy_mark = '🧦' if (acc['proxy_server'] or '').strip() else '🚫'
        lines.append(f'{status} {proxy_mark} {acc["email"]} | {acc["sign_time_sh"]} SH')
    send_msg(chat_id, '\n'.join(lines), reply_markup=account_list_markup(accounts, mode='manage'))


def handle_pending_text(chat_id: str, text: str, msg=None):
    pending = PENDING_ACTIONS.get(chat_id)
    if not pending:
        return False
    action = pending['action']
    if action == 'add_account':
        parsed = parse_account_text(text)
        if not parsed:
            PENDING_ACTIONS.pop(chat_id, None)
            send_msg(chat_id, '格式不对。\n\n本次添加已取消，请重新点击“添加账号”后再输入。\n\n正确格式：\n邮箱 密码 socks5代理\n\n格式示范：\nxxxxxxx@gmail.com your_password socks5://user:pass@1.2.3.4:1080', reply_markup=main_menu_markup(bool(list_accounts(chat_id)), chat_id == ADMIN_TG_ID))
            return True
        email, password, proxy, alias = parsed
        upsert_account(chat_id, email, password, proxy, alias)
        PENDING_ACTIONS.pop(chat_id, None)
        if msg:
            try_delete_user_message(msg)
        acc = get_account_by_email(chat_id, email)
        send_msg(chat_id, f'✅ 已保存账号：{email}', reply_markup=account_manage_markup(acc)[1])
        return True
    if action == 'set_time':
        if not is_hhmm(text.strip()):
            send_msg(chat_id, '时间格式错误，应为 HH:MM，例如 08:00')
            return True
        aid = pending['account_id']
        update_account(aid, 'sign_time_sh', text.strip())
        acc = get_account(aid)
        PENDING_ACTIONS.pop(chat_id, None)
        send_msg(chat_id, f'✅ {acc["email"]} 的签到时间已更新为 {text.strip()}（上海时间）', reply_markup=account_manage_markup(acc)[1])
        return True
    if action == 'set_proxy':
        aid = pending['account_id']
        raw = text.strip()
        value = None if raw.lower() == 'none' else raw
        if value and not is_valid_proxy(value):
            send_msg(chat_id, '代理格式不对。当前只支持 socks5://\n\n格式示范：\nsocks5://user:pass@1.2.3.4:1080')
            return True
        update_account(aid, 'proxy_server', value)
        acc = get_account(aid)
        PENDING_ACTIONS.pop(chat_id, None)
        if msg:
            try_delete_user_message(msg)
        send_msg(chat_id, f'✅ {acc["email"]} 的代理已更新。', reply_markup=account_manage_markup(acc)[1])
        return True
    return False


def handle_callback_query(cq):
    data = cq.get('data', '')
    callback_id = cq['id']
    msg = cq.get('message', {})
    chat_id = str(msg.get('chat', {}).get('id'))
    message_id = msg.get('message_id')
    from_user = cq.get('from', {})
    set_user(chat_id, from_user.get('username', ''))

    if data.startswith('admin:') and not is_admin_user(chat_id):
        answer_callback(callback_id, '没有权限')
        return

    if data == 'menu:home':
        answer_callback(callback_id)
        accounts = list_accounts(chat_id)
        edit_message(chat_id, message_id, help_text() + (f'\n\n你当前已添加 {len(accounts)} 个账号。' if accounts else ''), reply_markup=main_menu_markup(bool(accounts), chat_id == ADMIN_TG_ID))
        return
    if data == 'menu:add':
        answer_callback(callback_id, '请按提示发送账号信息')
        prompt_add_account(chat_id)
        return
    if data == 'menu:accounts':
        answer_callback(callback_id)
        accounts = list_accounts(chat_id)
        text = '📂 账号列表' if accounts else '你还没有添加账号。'
        if accounts:
            text += '\n\n说明：必须添加代理才可以进行签到。\n\n' + '\n'.join([f"{'✅' if a['enabled'] else '⛔️'} {'🧦' if (a['proxy_server'] or '').strip() else '🚫'} {a['email']} | {a['sign_time_sh']} SH" for a in accounts])
        edit_message(chat_id, message_id, text, reply_markup=account_list_markup(accounts, mode='manage') if accounts else main_menu_markup(False, chat_id == ADMIN_TG_ID))
        return
    if data == 'menu:status':
        answer_callback(callback_id)
        edit_message(chat_id, message_id, queue_status_text(chat_id), reply_markup=main_menu_markup(bool(list_accounts(chat_id)), is_admin_user(chat_id)))
        return

    if data == 'menu:admin':
        if not is_admin_user(chat_id):
            answer_callback(callback_id, '没有权限')
            return
        answer_callback(callback_id)
        edit_message(chat_id, message_id, admin_text(), reply_markup=admin_menu_markup())
        return

    if data == 'admin:users':
        answer_callback(callback_id)
        edit_message(chat_id, message_id, '👥 用户管理\n\n点击一个用户进入管理。', reply_markup=admin_users_markup(list_users()))
        return

    if data.startswith('admin:user:'):
        user_tg_id = data.split(':', 2)[2]
        row = get_user_row(user_tg_id)
        if not row:
            answer_callback(callback_id, '用户不存在')
            return
        answer_callback(callback_id)
        status = '✅ 可用' if row['enabled'] else '⛔️ 已禁用'
        account_count = count_accounts_for_user(user_tg_id)
        missing_proxy = count_missing_proxy_accounts_for_user(user_tg_id)
        text = (
            '👤 用户详情\n\n'
            f'TG ID：{row["tg_id"]}\n'
            f'用户名：{row["username"] or "未设置"}\n'
            f'状态：{status}\n'
            f'账号数：{account_count}\n'
            f'缺代理账号：{missing_proxy}'
        )
        edit_message(chat_id, message_id, text, reply_markup=admin_user_manage_markup(row))
        return

    if data.startswith('admin:accounts:'):
        user_tg_id = data.split(':', 2)[2]
        row = get_user_row(user_tg_id)
        if not row:
            answer_callback(callback_id, '用户不存在')
            return
        answer_callback(callback_id)
        accounts = list_accounts(user_tg_id)
        if not accounts:
            text = (
                '📂 用户账号列表\n\n'
                f'用户名：{row["username"] or "未设置"}\n'
                f'TG ID：{row["tg_id"]}\n\n'
                '这个用户还没有添加账号。'
            )
        else:
            lines = [
                '📂 用户账号列表',
                '',
                f'用户名：{row["username"] or "未设置"}',
                f'TG ID：{row["tg_id"]}',
                '',
            ]
            for acc in accounts[:50]:
                status = '✅' if acc['enabled'] else '⛔️'
                proxy_mark = '🧦' if (acc['proxy_server'] or '').strip() else '🚫'
                lines.append(f'{status} {proxy_mark} {acc["email"]} | {acc["sign_time_sh"]} SH')
            text = '\n'.join(lines)
        edit_message(chat_id, message_id, text, reply_markup={'inline_keyboard': [[{'text': '⬅️ 返回用户详情', 'callback_data': f'admin:user:{user_tg_id}'}], [{'text': '⬅️ 返回用户列表', 'callback_data': 'admin:users'}]]})
        return

    if data.startswith('admin:enable:'):
        user_tg_id = data.split(':', 2)[2]
        if user_tg_id == ADMIN_TG_ID:
            answer_callback(callback_id, 'admin 永远可用')
            return
        set_user_enabled(user_tg_id, 1)
        row = get_user_row(user_tg_id)
        answer_callback(callback_id, '已启用')
        text = (
            '👤 用户详情\n\n'
            f'TG ID：{row["tg_id"]}\n'
            f'用户名：{row["username"] or "未设置"}\n'
            f'状态：✅ 可用'
        )
        edit_message(chat_id, message_id, text, reply_markup=admin_user_manage_markup(row))
        return

    if data.startswith('admin:disable:'):
        user_tg_id = data.split(':', 2)[2]
        if user_tg_id == ADMIN_TG_ID:
            answer_callback(callback_id, '不能禁用 admin')
            return
        set_user_enabled(user_tg_id, 0)
        row = get_user_row(user_tg_id)
        answer_callback(callback_id, '已禁用')
        text = (
            '👤 用户详情\n\n'
            f'TG ID：{row["tg_id"]}\n'
            f'用户名：{row["username"] or "未设置"}\n'
            f'状态：⛔️ 已禁用'
        )
        edit_message(chat_id, message_id, text, reply_markup=admin_user_manage_markup(row))
        return
    if data == 'menu:sign_select':
        answer_callback(callback_id)
        accounts = list_accounts(chat_id)
        if not accounts:
            edit_message(chat_id, message_id, '你还没有添加账号。', reply_markup=main_menu_markup(False, chat_id == ADMIN_TG_ID))
            return
        edit_message(chat_id, message_id, '请选择要立即签到的账号：', reply_markup=account_list_markup(accounts, mode='sign'))
        return
    if data.startswith('account:'):
        answer_callback(callback_id)
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        _enabled_text, markup = account_manage_markup(acc)
        edit_message(chat_id, message_id, account_summary(acc), reply_markup=markup)
        return
    if data.startswith('sign:'):
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        allowed, reason = can_account_sign(acc)
        if not allowed:
            answer_callback(callback_id, '缺少代理，不能签到')
            send_msg(chat_id, f'⛔️ {mask_email(acc["email"])} 暂不能签到。\n\n原因：{reason}')
            return
        pos = enqueue_job(chat_id, aid, 'manual-button')
        answer_callback(callback_id, f'已加入队列，位置约 {pos}')
        send_msg(chat_id, f'▶️ 已提交签到：{mask_email(acc["email"])}\n当前排队位置约：{pos}')
        return
    if data.startswith('settime_prompt:'):
        aid = int(data.split(':', 1)[1])
        answer_callback(callback_id, '请发送新时间')
        prompt_settime(chat_id, aid)
        return
    if data.startswith('setproxy_prompt:'):
        aid = int(data.split(':', 1)[1])
        answer_callback(callback_id, '请发送代理')
        prompt_setproxy(chat_id, aid)
        return
    if data.startswith('enable:'):
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        update_account(aid, 'enabled', 1)
        acc = get_account(aid)
        answer_callback(callback_id, '已启用')
        _enabled_text, markup = account_manage_markup(acc)
        edit_message(chat_id, message_id, account_summary(acc), reply_markup=markup)
        return
    if data.startswith('disable:'):
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        update_account(aid, 'enabled', 0)
        acc = get_account(aid)
        answer_callback(callback_id, '已禁用')
        _enabled_text, markup = account_manage_markup(acc)
        edit_message(chat_id, message_id, account_summary(acc), reply_markup=markup)
        return
    if data.startswith('del_confirm:'):
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        answer_callback(callback_id)
        edit_message(chat_id, message_id, f'确认删除账号？\n\n{acc["email"]}', reply_markup={'inline_keyboard': [[{'text': '🗑 确认删除', 'callback_data': f'del:{aid}'}], [{'text': '取消', 'callback_data': f'account:{aid}'}]]})
        return
    if data.startswith('del:'):
        aid = int(data.split(':', 1)[1])
        acc = get_account(aid)
        if not acc or acc['tg_id'] != chat_id:
            answer_callback(callback_id, '账号不存在')
            return
        delete_account(aid)
        answer_callback(callback_id, '已删除')
        accounts = list_accounts(chat_id)
        edit_message(chat_id, message_id, '✅ 账号已删除', reply_markup=main_menu_markup(bool(accounts), chat_id == ADMIN_TG_ID))
        return
    answer_callback(callback_id)


def handle_message(msg):
    chat_id = str(msg['chat']['id'])
    username = msg.get('from', {}).get('username', '')
    text = (msg.get('text') or '').strip()
    if not text:
        return
    set_user(chat_id, username)

    if text.startswith('/admin'):
        if not is_admin_user(chat_id):
            send_msg(chat_id, '你没有权限使用这个命令。')
            return
        send_msg(chat_id, admin_text(), reply_markup=admin_menu_markup())
        return

    if not is_user_enabled(chat_id):
        send_msg(chat_id, '你当前已被禁用，无法使用这个机器人。')
        return

    if handle_pending_text(chat_id, text, msg):
        return

    if text.startswith('/start'):
        send_home(chat_id)
        return

    if text.startswith('/accounts'):
        send_accounts(chat_id)
        return

    if text.startswith('/status'):
        send_msg(chat_id, queue_status_text(chat_id), reply_markup=main_menu_markup(bool(list_accounts(chat_id)), is_admin_user(chat_id)))
        return

    if text.startswith('/signnow '):
        email = text.split(maxsplit=1)[1].strip()
        acc = get_account_by_email(chat_id, email)
        if not acc:
            send_msg(chat_id, '没找到这个邮箱对应的账号。')
            return
        allowed, reason = can_account_sign(acc)
        if not allowed:
            send_msg(chat_id, f'⛔️ {mask_email(acc["email"])} 暂不能签到。\n\n原因：{reason}')
            return
        pos = enqueue_job(chat_id, acc['id'], 'manual-email-command')
        send_msg(chat_id, f'▶️ 已提交签到：{mask_email(acc["email"])}\n当前排队位置约：{pos}')
        return

    parsed = parse_account_text(text)
    if parsed:
        email, password, proxy, alias = parsed
        upsert_account(chat_id, email, password, proxy, alias)
        try_delete_user_message(msg)
        acc = get_account_by_email(chat_id, email)
        send_msg(chat_id, f'✅ 已保存账号：{email}', reply_markup=account_manage_markup(acc)[1])
        return

    send_home(chat_id)


def run_scheduler():
    generate_daily_random_schedule()
    now = now_sh()
    today = now.date().isoformat()
    deadline_dt = sh_dt_for_minutes(now.date(), parse_hhmm_minutes(RENEW_DEADLINE_SH, '22:45'))
    rows = get_auto_accounts()
    for acc in rows:
        if account_success_date(acc) == today:
            continue
        next_dt = parse_sh_iso(acc['next_renew_at_sh'])
        force_deadline = now >= deadline_dt and acc['schedule_date_sh'] == today
        if not next_dt:
            continue
        if now < next_dt and not force_deadline:
            continue
        allowed, reason = can_account_sign(acc)
        display_email = mask_email(acc['email'])
        if not allowed:
            send_msg(acc['tg_id'], f'\u26d4\ufe0f {display_email} \u5230\u4e86\u8ba1\u5212\u65f6\u95f4\uff0c\u4f46\u6682\u4e0d\u80fd\u7eed\u671f\n\n\u539f\u56e0\uff1a{reason}')
            schedule_account_retry(acc['id'], reason)
            continue
        reason_text = 'deadline-urgent' if force_deadline else f'random@{next_dt.strftime("%H:%M")}(SH)'
        pos = enqueue_scheduled_account(acc, reason_text)
        if pos is None:
            continue
        send_msg(
            acc['tg_id'],
            f'\u23f0 \u5230\u8fbe\u7eed\u671f\u65f6\u95f4\uff1a{display_email}\n\u8ba1\u5212\u65f6\u95f4\uff1a{next_dt.strftime("%Y-%m-%d %H:%M:%S")}\uff08\u4e0a\u6d77\uff09\n\u5df2\u52a0\u5165\u961f\u5217\uff0c\u4f4d\u7f6e\u7ea6\uff1a{pos}'
        )
    send_daily_summaries_if_due()


def main():
    if not BOT_TOKEN:
        raise SystemExit('TG_BOT_TOKEN missing')
    init_db()
    offset = 0
    while True:
        try:
            res = tg_api('getUpdates', {'timeout': POLL_TIMEOUT, 'offset': offset})
            for item in res.get('result', []):
                offset = item['update_id'] + 1
                if 'message' in item:
                    handle_message(item['message'])
                elif 'callback_query' in item:
                    handle_callback_query(item['callback_query'])
            run_scheduler()
        except Exception:
            time.sleep(3)


if __name__ == '__main__':
    main()
