"""メール送信と、パスワードの安全な保存(Windows 資格情報マネージャー)。

パスワードは設定ファイルには書かず、keyring 経由で OS の資格情報ストアに保存する。
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import unicodedata
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate

from . import APP_ID

logger = logging.getLogger(__name__)

EMAIL_SUBJECT = "株価アラート"
KEYRING_SERVICE = APP_ID


class PasswordStoreError(Exception):
    """パスワードの保存・読み出しに失敗した。"""


def normalize_password(password: str | None) -> str:
    """貼り付け時に紛れ込む空白(全角スペース・改行・ノーブレークスペースなど)を取り除く。

    Google のアプリパスワードは「abcd efgh ijkl mnop」のように空白入りで表示されるが、空白は不要。
    """
    if not password:
        return ""
    text = unicodedata.normalize("NFKC", password)
    return "".join(ch for ch in text if not ch.isspace())


def _describe_error(exc: Exception, host: str, port: int, stage: str) -> str:
    """送信エラーを、原因の見当がつく日本語の説明にする。"""
    where = f"{host}:{port} への{stage}"
    gmail_hint = "Gmail の場合は SMTPサーバー smtp.gmail.com・ポート 587・送信元は Gmail アドレス、の組み合わせか確認してください。"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"メールサーバーへのログインに失敗しました({where})。"
                f"送信元メールアドレスとアプリパスワードを確認してください。{gmail_hint} 詳細: {exc}")
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return (f"メールサーバーとの接続が途中で切れました({where})。{gmail_hint}"
                "ウイルス対策ソフトの「メール保護」機能が通信を止めていることもあります。"
                "ログイン失敗が続いた直後は、しばらく(数十分)待ってから再度お試しください。 詳細: {exc}".format(exc=exc))
    if isinstance(exc, ssl.SSLError):
        return (f"暗号化通信に失敗しました({where})。ポート番号(587 / 465)が正しいか、"
                f"ウイルス対策ソフトが通信を検査していないか確認してください。 詳細: {exc}")
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, TimeoutError, socket.timeout)):
        return (f"メールサーバーに接続できませんでした({where})。SMTPサーバー名・ポート番号と"
                f"インターネット接続を確認してください。 詳細: {exc!r}")
    return f"メールの送信に失敗しました({where})。 詳細: {exc!r}"


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
    password = normalize_password(password)

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

    # どの段階で失敗したかをログに残し、原因を特定しやすくする
    stage = "接続"
    try:
        # 465 は最初から暗号化(SSL)、それ以外(587 など)は STARTTLS で暗号化する
        smtp_class = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with smtp_class(host, port, timeout=30) as smtp:
            if port != 465:
                stage = "暗号化(STARTTLS)"
                smtp.starttls()
            stage = "ログイン"
            smtp.login(from_addr, password)
            stage = "送信"
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
    except Exception as exc:  # noqa: BLE001 - 通知失敗でアプリを落とさない
        logger.error(_describe_error(exc, host, port, stage))
        return False

    logger.info("メールを送信しました(宛先: %s)", to_addr)
    return True
