from __future__ import annotations

import json
import unittest
from uuid import UUID

from cryptography.fernet import Fernet
import httpx


TEST_NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000000101")
TEST_EVENT_ID = UUID("00000000-0000-0000-0000-000000000102")


def _module():
    from app.services import alarm_http_notifications

    return alarm_http_notifications


def _draft(**changes):
    module = _module()
    values = {
        "name": "值班群通知",
        "description": None,
        "method": "POST",
        "url": "https://receiver.invalid/hook?token=hidden",
        "query_params": (),
        "headers": (),
        "content_type": "application/json",
        "body_template": '{"id":{{notification.id}},"value":{{entity.value}}}',
        "timeout_seconds": 5,
    }
    values.update(changes)
    return module.HttpNotificationDraft(**values)


def _context(**changes):
    module = _module()
    values = {
        "notification.id": str(TEST_NOTIFICATION_ID),
        "event.id": str(TEST_EVENT_ID),
        "event.type": "ALARM_ACTIVATED",
        "event.time": "2026-09-02T10:00:00+00:00",
        "alarm.name": "PCS 故障",
        "alarm.severity": "MAJOR",
        "alarm.state": "active_unacknowledged",
        "alarm.definition_id": "definition-1",
        "alarm.rule_key": "pcs.fault",
        "node.id": "node-1",
        "node.name": "1# PCS",
        "node.path": "场站/储能/1# PCS",
        "entity.id": "entity-1",
        "entity.key": "pcs.fault",
        "entity.name": "PCS 故障",
        "entity.value": 12.5,
        "entity.unit": None,
        "entity.quality": 192,
        "entity.observed_at": "2026-09-02T10:00:00+00:00",
    }
    values.update(changes)
    return module.NotificationContext(values)


class AlarmHttpNotificationContractTest(unittest.TestCase):
    def test_json_template_renders_typed_values_and_system_headers(self) -> None:
        module = _module()
        rendered = module.render_request(module.normalize_draft(_draft()), _context())

        self.assertEqual(
            {"id": str(TEST_NOTIFICATION_ID), "value": 12.5},
            json.loads(rendered.body),
        )
        self.assertEqual(str(TEST_NOTIFICATION_ID), rendered.headers["Idempotency-Key"])
        self.assertEqual(
            str(TEST_NOTIFICATION_ID),
            rendered.headers["X-ZiZu-Notification-Id"],
        )
        self.assertEqual("application/json", rendered.headers["Content-Type"])

    def test_unknown_template_variable_is_rejected(self) -> None:
        module = _module()
        with self.assertRaises(module.HttpNotificationError) as raised:
            module.normalize_draft(_draft(body_template="{{system.password}}"))
        self.assertEqual("HTTP_NOTIFICATION_INVALID_TEMPLATE", raised.exception.code)

    def test_invalid_json_template_is_rejected_before_delivery(self) -> None:
        module = _module()
        with self.assertRaises(module.HttpNotificationError) as raised:
            module.normalize_draft(_draft(body_template='{"value":{{entity.value}}'))
        self.assertEqual("HTTP_NOTIFICATION_INVALID_TEMPLATE", raised.exception.code)

    def test_link_local_and_userinfo_targets_are_rejected(self) -> None:
        module = _module()
        for url in (
            "http://169.254.169.254/latest/meta-data",
            "http://user:password@receiver.invalid/hook",
            "file:///tmp/hook",
        ):
            with self.subTest(url=url), self.assertRaises(module.HttpNotificationError) as raised:
                module.normalize_draft(_draft(url=url))
            self.assertEqual("HTTP_NOTIFICATION_INVALID_URL", raised.exception.code)

    def test_secret_codec_never_falls_back_to_plaintext(self) -> None:
        module = _module()
        codec = module.SecretCodec(None)
        with self.assertRaises(module.HttpNotificationError) as raised:
            codec.encrypt("https://receiver.invalid/hook?token=hidden")
        self.assertEqual(
            "HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED",
            raised.exception.code,
        )

        key = Fernet.generate_key().decode("ascii")
        secured = module.SecretCodec(key)
        encrypted = secured.encrypt("Bearer hidden")
        self.assertNotIn("hidden", encrypted)
        self.assertEqual("Bearer hidden", secured.decrypt(encrypted))

    def test_public_target_masks_query_values(self) -> None:
        module = _module()
        display = module.mask_url(
            "https://receiver.invalid/open-apis/bot/hook/path-hidden?token=hidden&room=ops"
        )
        self.assertEqual(
            "https://receiver.invalid/***?token=%2A%2A%2A&room=%2A%2A%2A",
            display,
        )


class AlarmHttpNotificationSendTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_2xx_is_delivered_and_redirect_is_not_followed(self) -> None:
        module = _module()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://other.invalid"})

        result = await module.send_http_request(
            module.render_request(module.normalize_draft(_draft()), _context()),
            transport=httpx.MockTransport(handler),
        )
        self.assertFalse(result.delivered)
        self.assertEqual(302, result.http_status)
        self.assertEqual("rejected", result.outcome)
        self.assertEqual("HTTP_NOTIFICATION_DELIVERY_REJECTED", result.error_code)

    async def test_timeout_has_stable_redacted_result(self) -> None:
        module = _module()

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("secret target timed out", request=request)

        result = await module.send_http_request(
            module.render_request(module.normalize_draft(_draft()), _context()),
            transport=httpx.MockTransport(handler),
        )
        self.assertFalse(result.delivered)
        self.assertEqual("timeout", result.outcome)
        self.assertEqual("HTTP_NOTIFICATION_DELIVERY_TIMEOUT", result.error_code)
        self.assertNotIn("secret", result.error_detail or "")

    async def test_response_excerpt_is_sanitized_and_limited_to_4096_bytes(self) -> None:
        module = _module()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok\x00" + "界" * 2000)

        result = await module.send_http_request(
            module.render_request(module.normalize_draft(_draft()), _context()),
            transport=httpx.MockTransport(handler),
        )
        self.assertTrue(result.delivered)
        self.assertNotIn("\x00", result.response_excerpt or "")
        self.assertLessEqual(len((result.response_excerpt or "").encode("utf-8")), 4096)


if __name__ == "__main__":
    unittest.main()
