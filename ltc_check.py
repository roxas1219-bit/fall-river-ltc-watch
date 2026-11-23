#!/usr/bin/env python3
"""Public Fall River LTC booking checker for a GitHub Actions cloud runner.

Uses real Chromium JavaScript rendering and checks for an enabled, visible
appointment TIME after selecting a visible, enabled DATE. It never books.

Security and reliability: does NOT bypass CAPTCHA/Cloudflare, query hidden APIs,
log in, or assume an unreadable page means there are no appointments.
Live Fall River DOM integration is NOT confirmed; see README before relying on it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BOOKING_URL = 'https://calendly.com/fallriverpd/ltc'
STATE_PATH = Path(os.getenv('WATCH_STATE_FILE', '.ltc-state/state.json'))
MAX_MONTHS = 3  # A 45-day window can touch three named calendar months.
MAX_DATES_PER_MONTH = 8  # Be conservative about how much UI we click per check.
OPEN_REMINDER_HOURS = 1
ISSUE_REMINDER_HOURS = 6

DATE_BUTTON_SELECTORS = (
    'button[data-container="date-cell"]',
    '[data-container="date-cell"] button',
    'button[data-testid="day-button"]',
    '[data-testid="calendar"] button',
    '[role="grid"] button',
    '[data-container="calendar"] button',
)
NEXT_MONTH_SELECTORS = (
    'button[aria-label="Next month"]',
    'button[aria-label="Go to next month"]',
    'button[title="Next month"]',
    'button[data-container="next-month-button"]',
    '[data-testid="calendar-next-button"]',
    'button:has-text("Next month")',
)
CALENDAR_SELECTORS = (
    '[data-container="calendar"]',
    '[data-testid="calendar"]',
    '[role="grid"]',
)
TIME_RE = re.compile(r'^\s*(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:[AaPp]\.?[Mm]\.?)?\s*$')
DAY_RE = re.compile(r'^\s*(?:[1-9]|[12]\d|3[01])\s*$')
TOPIC_RE = re.compile(r'^[A-Za-z0-9_-]{12,100}$')
CHALLENGE_TITLE_RE = re.compile(r'just a moment|attention required|access denied|verify you are human', re.I)
CHALLENGE_BODY_RE = re.compile(
    r'checking your browser before accessing|verify (that )?you are human|'
    r'complete the security check|cloudflare ray id', re.I
)
CHALLENGE_SELECTORS = (
    '#challenge-form',
    'iframe[src*="challenges.cloudflare.com"]',
    'input[name="cf-turnstile-response"]',
    '[id^="cf-chl"]',
)


def timestamp() -> datetime:
    return datetime.now(timezone.utc)


def now() -> str:
    return datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %I:%M %p ET')


def print_status(state: str, message: str) -> None:
    safe = message.replace('\n', ' ').strip()
    print(f'{state} | {now()} | {safe}', flush=True)
    summary_path = os.getenv('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as f:
            f.write(f'### LTC calendar: {state}\n\n{safe}\n\n')
            f.write(f'[Open official booking page]({BOOKING_URL})\n')


def notify(title: str, message: str) -> None:
    topic = os.getenv('NTFY_TOPIC', '').strip()
    if not TOPIC_RE.fullmatch(topic):
        raise RuntimeError('Missing/invalid NTFY_TOPIC; set a long random topic as a GitHub Actions secret.')
    req = urllib.request.Request(
        'https://ntfy.sh/' + urllib.parse.quote(topic, safe=''),
        method='POST', data=message.encode('utf-8'),
        headers={
            'Content-Type': 'text/plain; charset=utf-8',
            'Title': title,
            'Priority': 'high',
            'Click': BOOKING_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f'ntfy HTTP {response.status}')
    print('Phone alert sent (topic value not logged).', flush=True)


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if isinstance(data, dict) and data.get('version') == 1:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {'version': 1}


def save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(STATE_PATH)


def within_cooldown(iso: str | None, hours: int, current: datetime) -> bool:
    if not iso:
        return False
    try:
        last = datetime.fromisoformat(iso)
        if last.tzinfo is None:
            return False
        return last <= current < last + timedelta(hours=hours)
    except ValueError:
        return False


def alert_decision(previous: dict, result: str, detail: str, current: datetime) -> tuple[bool, str]:
    """Decide alerts from a restored prior-run state; no periodic error spam."""
    prev_result = previous.get('result')
    prev_detail = previous.get('detail', '')
    prev_alert = previous.get('alerted_at')
    if result == 'OPEN':
        changed = result != prev_result or detail != prev_detail
        return changed or not within_cooldown(prev_alert, OPEN_REMINDER_HOURS, current), 'OPEN'
    if result in ('BLOCKED', 'ERROR', 'UNKNOWN'):
        # Alert immediately on a new problem, changed HTTP/challenge error,
        # and once every six hours for persistent issues.
        changed = result != prev_result or detail != prev_detail
        return changed or not within_cooldown(prev_alert, ISSUE_REMINDER_HOURS, current), 'ISSUE'
    if result == 'NONE' and prev_result in ('BLOCKED', 'ERROR', 'UNKNOWN'):
        return True, 'RECOVERED'
    return False, ''


def handle_result(result: str, detail: str, *, send=notify, current: datetime | None = None) -> int:
    print_status(result, detail)
    current = current or timestamp()
    previous = load_state()
    should_send, alert_type = alert_decision(previous, result, detail, current)

    # On a recurring problem, a dynamic detail (e.g. a timestamp) would cause
    # spam. The caller must supply stable, descriptive error strings.
    state = {
        'version': 1,
        'result': result,
        'detail': detail,
        'checked_at': current.isoformat(),
    }
    if should_send:
        titles = {
            'OPEN': 'Fall River LTC: appointment detected',
            'ISSUE': f'Fall River LTC watcher: {result.lower()}',
            'RECOVERED': 'Fall River LTC watcher: recovered',
        }
        message = (detail if alert_type != 'RECOVERED' else
                   'The watcher can read the calendar again; no appointment was confirmed.')
        message += f'\nOpen the official page: {BOOKING_URL}'
        try:
            send(titles[alert_type], message)
            state['alerted_at'] = current.isoformat()
        except Exception as error:
            # Retain previous alert time so the next scheduled run retries.
            state['alerted_at'] = previous.get('alerted_at')
            save_state(state)
            print(f'ERROR: phone notification failed: {type(error).__name__}: {error}', file=sys.stderr)
            return 3
    else:
        state['alerted_at'] = previous.get('alerted_at')
    save_state(state)
    # Error conditions remain red in GitHub Actions and trigger diagnostics.
    return 0 if result in ('OPEN', 'NONE') else 2


def classify_http_status(status: int) -> str:
    if status == 200:
        return 'OK'
    if status in (401, 403, 429):
        return 'BLOCKED'
    return 'ERROR'


def is_challenge(page) -> bool:
    """Detect common bot challenges even when the HTTP response is 200."""
    try:
        if CHALLENGE_TITLE_RE.search(page.title()):
            return True
    except Exception:
        pass
    for selector in CHALLENGE_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
    try:
        body = page.locator('body').inner_text(timeout=1500)[:3500]
        if CHALLENGE_BODY_RE.search(body):
            return True
    except Exception:
        pass
    return False


def is_relevant_calendly_api_failure(url: str, status: int) -> bool:
    """Flag failed availability fetches; ignore unrelated images/analytics."""
    if status < 400:
        return False
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or '').lower()
    if hostname != 'calendly.com' and not hostname.endswith('.calendly.com'):
        return False
    return bool(re.search(r'availability|available.times|booking|scheduling', parsed.path, re.I))


def diagnostic(page) -> None:
    try:
        page.screenshot(path='diagnostic.png', full_page=True, timeout=6000)
    except Exception as error:
        print(f'Diagnostic screenshot unavailable: {type(error).__name__}', flush=True)


def is_date_cell(button) -> bool:
    try:
        if not button.is_visible(timeout=500):
            return False
        value = (button.inner_text(timeout=500) or '').strip()
        aria = (button.get_attribute('aria-label', timeout=500) or '').strip().lower()
        container = (button.get_attribute('data-container', timeout=500) or '').lower()
        if container == 'date-cell' or DAY_RE.fullmatch(value):
            return True
        return bool(('available' in aria or 'select' in aria) and re.search(r'\b\d{1,2}\b', aria))
    except Exception:
        return False


def date_label(button) -> str:
    for attr in ('aria-label', 'title', 'data-date', 'datetime'):
        try:
            value = (button.get_attribute(attr, timeout=700) or '').strip()
            if value:
                return value[:100]
        except Exception:
            pass
    try:
        text = button.inner_text(timeout=700).strip()
    except Exception:
        text = '?'
    return f'day {text} (verify month on booking page)'


def all_date_buttons(page):
    for selector in DATE_BUTTON_SELECTORS:
        try:
            buttons = page.locator(selector)
            found = [buttons.nth(i) for i in range(min(buttons.count(), 50))
                     if is_date_cell(buttons.nth(i))]
            if found:
                return found
        except Exception:
            pass
    return []


def confirmed_times(page) -> list[str]:
    """Only visible enabled buttons after a date click, never text on page."""
    out = []
    try:
        buttons = page.get_by_role('button')
        for i in range(min(buttons.count(), 100)):
            button = buttons.nth(i)
            try:
                if button.is_visible(timeout=500) and button.is_enabled(timeout=500):
                    label = (button.inner_text(timeout=500) or '').strip()
                    if TIME_RE.fullmatch(label):
                        out.append(label)
            except Exception:
                continue
    except Exception:
        pass
    return list(dict.fromkeys(out))


def click_next(page) -> bool:
    for selector in NEXT_MONTH_SELECTORS:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible(timeout=600) and button.is_enabled(timeout=600):
                button.click(timeout=4000)
                page.wait_for_timeout(950)
                return True
        except Exception:
            pass
    return False


def wait_for_calendar(page) -> bool:
    for selector in CALENDAR_SELECTORS:
        try:
            page.locator(selector).first.wait_for(state='visible', timeout=5000)
            return True
        except Exception:
            continue
    return False


def wait_for_time_buttons(page) -> list[str]:
    # Dynamic Calendly fetches can finish well AFTER the initial page loads.
    # Poll for up to ~5 seconds instead of assuming a fixed 1-second delay.
    for _ in range(6):
        times = confirmed_times(page)
        if times:
            return times
        page.wait_for_timeout(850)
    return []


def check_calendar() -> tuple[str, str]:
    """OPEN requires an actual time button; unreadable is NEVER no slots."""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    locale='en-US', timezone_id='America/New_York',
                    viewport={'width': 1280, 'height': 900},
                )
                page = context.new_page()
                api_failures: list[tuple[int, str]] = []

                def monitor_response(response):
                    try:
                        if is_relevant_calendly_api_failure(response.url, response.status):
                            api_failures.append((response.status, urllib.parse.urlparse(response.url).path[:100]))
                    except Exception:
                        pass

                page.on('response', monitor_response)
                try:
                    response = page.goto(BOOKING_URL, wait_until='domcontentloaded', timeout=35000)
                    if response is None:
                        diagnostic(page)
                        return 'ERROR', 'Main booking page did not provide an HTTP response.'
                    status = response.status
                    if status != 200:
                        diagnostic(page)
                        return classify_http_status(status), f'Main Calendly page returned HTTP {status} (not 200).'
                    if is_challenge(page):
                        diagnostic(page)
                        return 'BLOCKED', 'Browser challenge detected on Calendly (HTTP 200 does not mean readable).'
                    if not wait_for_calendar(page):
                        diagnostic(page)
                        if is_challenge(page):
                            return 'BLOCKED', 'Browser challenge detected after JavaScript loaded.'
                        if api_failures:
                            code, endpoint = api_failures[0]
                            return classify_http_status(code), f'Calendly availability request returned HTTP {code}: {endpoint}'
                        return 'UNKNOWN', 'Calendar did not render within the allowed loading time.'
                    if is_challenge(page):
                        diagnostic(page)
                        return 'BLOCKED', 'Browser challenge appeared while calendar was rendering.'
                    if api_failures:
                        code, endpoint = api_failures[0]
                        diagnostic(page)
                        return classify_http_status(code), f'Calendly availability request returned HTTP {code}: {endpoint}'

                    visible_days = 0
                    any_enabled = False
                    for month in range(MAX_MONTHS):
                        page.wait_for_timeout(650)
                        if is_challenge(page):
                            diagnostic(page)
                            return 'BLOCKED', 'Browser challenge appeared during calendar inspection.'
                        days = all_date_buttons(page)
                        if not days:
                            diagnostic(page)
                            return 'UNKNOWN', 'Calendar was visible, but readable date buttons were not found.'
                        visible_days += len(days)
                        enabled_indexes = []
                        for idx, day in enumerate(days):
                            try:
                                if day.is_enabled(timeout=500) and day.get_attribute('aria-disabled') != 'true':
                                    enabled_indexes.append(idx)
                            except Exception:
                                pass
                        print(f'Month {month + 1}/{MAX_MONTHS}: {len(days)} visible day buttons, '
                              f'{len(enabled_indexes)} enabled.', flush=True)
                        if enabled_indexes:
                            any_enabled = True
                        for idx in enabled_indexes[:MAX_DATES_PER_MONTH]:
                            current = all_date_buttons(page)  # re-acquire after each re-render
                            if idx >= len(current):
                                break
                            day = current[idx]
                            label = date_label(day)
                            try:
                                day.click(timeout=4500)
                                times = wait_for_time_buttons(page)
                                if api_failures:
                                    code, endpoint = api_failures[0]
                                    diagnostic(page)
                                    return classify_http_status(code), f'Calendly availability request returned HTTP {code}: {endpoint}'
                                if is_challenge(page):
                                    diagnostic(page)
                                    return 'BLOCKED', 'Browser challenge appeared after date selection.'
                                if times:
                                    return 'OPEN', f'Selectable date {label}; time button {times[0]} (Eastern Time)'
                            except Exception as error:
                                print(f'Date inspect failed for {label}: {type(error).__name__}', flush=True)
                        if month < MAX_MONTHS - 1 and not click_next(page):
                            diagnostic(page)
                            return 'UNKNOWN', 'Could not inspect subsequent months; no-availability result is unverified.'
                    if any_enabled:
                        diagnostic(page)
                        return 'UNKNOWN', 'Enabled date found but no visible, enabled time button could be verified.'
                    if not visible_days:
                        return 'UNKNOWN', 'No readable date buttons.'
                    return 'NONE', 'Three calendar months readable; no enabled dates were found.'
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception as error:
        return 'ERROR', f'Browser execution failed: {type(error).__name__}: {str(error)[:120]}'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--test-notification', action='store_true',
                        help='Test ntfy only; this does not test access to Calendly')
    args = parser.parse_args()
    if args.test_notification:
        try:
            notify('Fall River LTC watcher: test', 'Test notification works. This does NOT prove that Calendly can be read.')
            print_status('TEST OK', 'Phone test succeeded; calendar not checked.')
            return 0
        except Exception as error:
            print_status('TEST FAILED', f'Phone test failed: {type(error).__name__}: {error}')
            return 1
    return handle_result(*check_calendar())


if __name__ == '__main__':
    sys.exit(main())
