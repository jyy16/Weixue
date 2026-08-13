"""Feishu bot (IM) integration: message/card sending + event/card callback handling.

APIs (verified 2026-08):
- Send message: POST /open-apis/im/v1/messages?receive_id_type=open_id
- Reply:        POST /open-apis/im/v1/messages/{message_id}/reply
- Event callbacks: answer `challenge`, verify token and (when Encrypt Key is
  configured) the sha256 request signature.
- Card callbacks: verify the sha1 request signature against the Verification
  Token, decrypt the body when encrypted, and dispatch button actions.

Rate limits: 1000 req/min, 50 req/s overall; 5 QPS per user - batch sends carefully.
"""

import base64
import hashlib
import hmac
import json
from typing import Any, Optional

from .client import FeishuClient, FeishuConfig

SEND_MESSAGE_PATH = "/im/v1/messages"
REPLY_MESSAGE_PATH = "/im/v1/messages/{message_id}/reply"


class BotService:
    def __init__(self, client: FeishuClient) -> None:
        self.client = client

    async def send_text(self, open_id: str, text: str) -> Any:
        return await self.client.request(
            "POST",
            SEND_MESSAGE_PATH,
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def send_card(self, open_id: str, card: dict) -> Any:
        return await self.client.request(
            "POST",
            SEND_MESSAGE_PATH,
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )

    async def reply_text(self, message_id: str, text: str) -> Any:
        return await self.client.request(
            "POST",
            REPLY_MESSAGE_PATH.format(message_id=message_id),
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    @staticmethod
    def verify_card_signature(
        config: FeishuConfig,
        raw_body: bytes,
        timestamp: str,
        nonce: str,
        signature: str,
    ) -> bool:
        """Verify the `X-Lark-Signature` header of an interactive-card callback.

        Algorithm (from the official Python SDK CardActionHandler): the digest is
        sha1 of ``timestamp + nonce + verification_token + raw_body`` (hex).
        The signature is only sent when a callback request address is configured;
        when no Verification Token is set we reject rather than silently accept.
        """
        if not config.verification_token:
            return False
        if not raw_body or not timestamp or not nonce or not signature:
            return False
        bs = (
            f"{timestamp}{nonce}{config.verification_token}".encode("utf-8")
            + raw_body
        )
        digest = hashlib.sha1(bs).hexdigest()
        return hmac.compare_digest(digest, signature)

    @staticmethod
    def decrypt_card_payload(config: FeishuConfig, raw_body: bytes) -> bytes:
        """Return the plaintext card callback body (AES-decrypted when encrypted)."""
        if not raw_body:
            raise ValueError("empty card callback body")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("card callback body is not valid JSON") from exc
        if parsed.get("encrypt"):
            inner = BotService._decrypt_event(config.encrypt_key, parsed["encrypt"])
            return inner.encode("utf-8")
        return raw_body

    @staticmethod
    def build_comment_card(
        *,
        title: str,
        content: str,
        course_id: int,
        student_id: int,
        response_id: int,
        comment_hash: str = "",
        change_url: str = "",
    ) -> dict:
        """Build a Feishu interactive card (schema 2.0) with the three workflow
        buttons: confirm review / change on web / send to student.

        Button ``value`` carries the entity ids plus an ``action`` discriminator
        that the card callback dispatches on.

        When ``change_url`` is provided the middle "去网页修改" button is a plain
        URL jump button that opens the web grading page directly (needs no
        callback infrastructure). Otherwise it falls back to a callback button.
        """
        base = {
            "course_id": course_id,
            "student_id": student_id,
            "response_id": response_id,
        }
        if comment_hash:
            base["comment_hash"] = comment_hash
        if change_url:
            change_button = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "去网页修改",
                },
                "url": change_url,
            }
        else:
            change_button = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "去网页修改",
                },
                "value": {**base, "action": "request_change"},
            }
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {"tag": "markdown", "content": content},
                    # Schema 2.0 dropped the "action" container: buttons sit
                    # directly in body.elements (live API error 200861 otherwise).
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {
                            "tag": "plain_text",
                            "content": "确认评分",
                        },
                        "value": {**base, "action": "review_confirm"},
                    },
                    change_button,
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "发送给学生",
                        },
                        "value": {**base, "action": "send_comment"},
                    },
                ],
            },
        }

    @staticmethod
    def build_student_comment_card(
        *,
        student_name: str,
        comment: str,
    ) -> dict:
        """Build the read-only card delivered to one bound student account."""
        safe_name = (student_name or "同学").strip()
        safe_comment = (comment or "").strip()
        return {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"思辨星 · {safe_name}的课堂反馈",
                },
                "template": "green",
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": safe_comment,
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": "这份反馈已经过老师确认，请继续保持思考与表达。",
                        },
                    },
                ],
            },
        }

    @staticmethod
    def build_prep_plan_card(
        *,
        title: str,
        content: str,
        course_id: int,
        change_url: str = "",
    ) -> dict:
        """Build a Feishu interactive card (schema 2.0) for the lesson-prep plan.

        Shows the teacher's saved plan (order + weak dimensions + notes) with a
        URL jump back to the web prep page and a "确认计划" callback button
        (dispatched as ``prep_confirm`` by card_actions).
        """
        base = {"course_id": course_id}
        if change_url:
            change_button = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "去网页调整",
                },
                "url": change_url,
            }
        else:
            change_button = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "去网页调整",
                },
                "value": {**base, "action": "prep_open"},
            }
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "indigo",
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {"tag": "markdown", "content": content},
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {
                            "tag": "plain_text",
                            "content": "确认计划",
                        },
                        "value": {**base, "action": "prep_confirm"},
                    },
                    change_button,
                ],
            },
        }

    @staticmethod
    def handle_event(config: FeishuConfig, body: dict) -> dict:
        """Validate a Feishu event callback payload.

        Returns {"challenge": ...} for url_verification, or the (decrypted) inner
        event body for real events. Raises ValueError when the token mismatches.
        """
        if not isinstance(body, dict):
            raise ValueError("invalid event payload")
        if body.get("encrypt"):
            inner = BotService._decrypt_event(config.encrypt_key, body["encrypt"])
            body = json.loads(inner)
        token = body.get("token", "")
        if config.verification_token and token != config.verification_token:
            raise ValueError("event verification token mismatch")
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", "")}
        return body

    @staticmethod
    def _decrypt_event(encrypt_key: str, payload_b64: str) -> str:
        """AES-256-CBC decrypt of Feishu encrypted event payloads.

        Feishu uses the Encrypt Key directly: key = first 32 bytes of the key
        string, iv = first 16 bytes. Requires the `cryptography` package.
        """
        if not encrypt_key:
            raise ValueError("FEISHU_ENCRYPT_KEY not configured but event is encrypted")
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise ValueError(
                "cryptography not installed; required when FEISHU_ENCRYPT_KEY is set"
            )
        key = encrypt_key.encode("utf-8")[:32]
        iv = key[:16]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        plain = decryptor.update(base64.b64decode(payload_b64)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(plain) + unpadder.finalize()).decode("utf-8")
