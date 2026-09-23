"""メール送信と、パスワードの安全な保存(Windows 資格情報マネージャー)。

パスワードは設定ファイルには書かず、keyring 経由で OS の資格情報ストアに保存する。
"""

from __future__ import annotations

import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate

from . import APP_ID

logger = logging.getLogger(__name__)

EMAIL_SUBJECT = "株価アラート"
KEYRING_SERVICE = APP_ID


class PasswordStoreError(Exception):
    """パスワードの保存・読み出しに失敗した。"""


def get_password(user: str) -> str | None:
    if not user:
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存済みパスワードを読み出せませんでした: %s", exc)
        return None


def set_password(user: str, password: str) -> None:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, user, password)
    except Exception as exc:  # noqa: BLE001
        raise PasswordStoreError(str(exc)) from exc


def missing_fields(email_cfg: dict, password: str | None) -> list[str]:
    """メール送信に足りない設定項目の名前(画面表示用)を返す。"""
    missing = []
    if not str(email_cfg.get("smtp_host") or "").strip():
        missing.append("SMTPサーバー")
    if not str(email_cfg.get("from_addr") or "").strip():
        missing.append("送信元メールアドレス")
    if not password:
        missing.append("アプリパスワード")
    return missing


def send_email(
    text: str,
    email_cfg: dict,
    password: str | None = None,
    subject: str = EMAIL_SUBJECT,
) -> bool:
    """メールを送信し、成功したかどうかを返す(例外は送出しない)。

    password を省略した場合は資格情報マネージャーから読み出す。
    送信先が空の場合は送信元アドレス(自分自身)に送る。
    """
    from_addr = str(email_cfg.get("from_addr") or "").strip()
    to_addr = str(email_cfg.get("to_addr") or "").strip() or from_addr
    host = str(email_cfg.get("smtp_host") or "").strip()
    if password is None:
        password = get_password(from_addr)

    missing = missing_fields(email_cfg, password)
    if missing:
        logger.warning("メール設定が不足しているため送信をスキップします(未設定: %s)", "、".join(missing))
        return False

    try:
        port = int(email_cfg.get("smtp_port") or 587)
    except (TypeError, ValueError):
        logger.error("SMTPポートの値が不正です: %r", email_cfg.get("smtp_port"))
        return False

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)

    try:
        # 465 は最初から暗号化(SSL)、それ以外(587 など)は STARTTLS で暗号化する
        smtp_class = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with smtp_class(host, port, timeout=30) as smtp:
            if port != 465:
                smtp.starttls()
            smtp.login(from_addr, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("メールサーバーへのログインに失敗しました。アドレスとアプリパスワードを確認してください: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - 通知失敗でアプリを落とさない
        logger.error("メールの送信に失敗しました: %s", exc)
        return False

    logger.info("メールを送信しました(宛先: %s)", to_addr)
    return True
