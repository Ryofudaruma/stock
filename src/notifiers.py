"""LINE / メール送信処理。

どの関数も例外を送出せず、送信に成功したかどうかを真偽値で返す。
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate

import requests

logger = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MAX_TEXT_LENGTH = 4900  # API上限は5000文字。安全のため4900文字で切り詰める
EMAIL_SUBJECT = "株価アラート"


def truncate_for_line(text: str) -> str:
    """LINE の文字数上限に収まるように本文を切り詰める。"""
    return text[:LINE_MAX_TEXT_LENGTH]


def send_line(text: str) -> bool:
    """LINE Messaging API の push message で本文を送信する。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        logger.warning(
            "LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定のため、LINE通知をスキップします"
        )
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "to": user_id,
        "messages": [{"type": "text", "text": truncate_for_line(text)}],
    }
    try:
        response = requests.post(LINE_PUSH_URL, headers=headers, json=body, timeout=15)
    except Exception as exc:  # noqa: BLE001 - 通知失敗でプロセスを落とさない
        logger.error("LINE通知の送信中にエラーが発生しました: %s", exc)
        return False

    if response.status_code == 200:
        logger.info("LINE通知を送信しました")
        return True

    logger.error(
        "LINE通知の送信に失敗しました: HTTP %s %s", response.status_code, response.text
    )
    return False


def send_email(text: str) -> bool:
    """SMTP (STARTTLS) でメールを送信する。"""
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port_str = os.environ.get("SMTP_PORT") or "587"
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO")

    if not user or not password or not to_addr:
        logger.warning(
            "SMTP_USER / SMTP_PASSWORD / EMAIL_TO のいずれかが未設定のため、メール通知をスキップします"
        )
        return False

    try:
        port = int(port_str)
    except ValueError:
        logger.error("SMTP_PORT の値が不正です: %r", port_str)
        return False

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = str(Header(EMAIL_SUBJECT, "utf-8"))
    msg["From"] = user
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, [to_addr], msg.as_string())
    except Exception as exc:  # noqa: BLE001 - 通知失敗でプロセスを落とさない
        logger.error("メール通知の送信に失敗しました: %s", exc)
        return False

    logger.info("メール通知を送信しました")
    return True
