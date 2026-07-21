from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send_quiz_email_code(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    starttls: bool,
    recipient: str,
    code: str,
    campaign_title: str,
    expires_minutes: int,
) -> None:
    message = EmailMessage()
    message["Subject"] = f"Код входа в квиз Hi, Jack!: {code}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"Код подтверждения: {code}\n\n"
        f"Квиз: {campaign_title}\n"
        f"Код действует {expires_minutes} минут. Если вы не запрашивали код, просто проигнорируйте письмо."
    )
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if starttls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
