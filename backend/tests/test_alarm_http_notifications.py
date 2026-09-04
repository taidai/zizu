from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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

    def test_json_template_can_render_entity_value_as_text(self) -> None:
        module = _module()
        rendered = module.render_request(
            module.normalize_draft(
                _draft(body_template='{"content":{{entity.value_text}}}')
            ),
            _context(**{"entity.value": False}),
        )

        self.assertEqual({"content": "false"}, json.loads(rendered.body))

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

    async def test_feishu_business_error_is_not_delivered_despite_http_200(self) -> None:
        module = _module()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 11246, "msg": "parse card json error"},
            )

        request = module.render_request(
            module.normalize_draft(
                _draft(
                    url="https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
                )
            ),
            _context(),
        )
        result = await module.send_http_request(
            request,
            transport=httpx.MockTransport(handler),
        )

        self.assertFalse(result.delivered)
        self.assertEqual("rejected", result.outcome)
        self.assertEqual("HTTP_NOTIFICATION_DELIVERY_REJECTED", result.error_code)
        self.assertEqual(200, result.http_status)

    async def test_feishu_code_zero_is_delivered(self) -> None:
        module = _module()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "msg": "success"})

        request = module.render_request(
            module.normalize_draft(
                _draft(
                    url="https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
                )
            ),
            _context(),
        )
        result = await module.send_http_request(
            request,
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.delivered)
        self.assertEqual("delivered", result.outcome)


class _DeliveryRepository:
    def __init__(self, results) -> None:
        module = _module()
        self.now = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
        self.config = module.ResolvedHttpNotificationConfig(
            TEST_NOTIFICATION_ID,
            _draft(url="https://old.invalid/hook"),
            "a" * 64,
            "a" * 64,
            True,
        )
        self.claim = module.DeliveryClaim(
            id=TEST_NOTIFICATION_ID,
            transition_id=UUID("00000000-0000-0000-0000-000000000103"),
            transition_code="ALARM_ACTIVATED",
            event_id=TEST_EVENT_ID,
            configuration_id=TEST_NOTIFICATION_ID,
            context=_context(),
            attempt_count=0,
            cycle_attempt_count=0,
            lease_owner="",
        )
        self.status = "pending"
        self.next_at = self.now
        self.results = list(results)
        self.attempts = []

    def claim_due(self, *, worker_id, now, lease_seconds=30):
        if (
            self.status not in {"pending", "retry_wait"}
            or now < self.next_at
            or not self.config.enabled
            or self.config.tested_digest != self.config.current_digest
        ):
            return None
        self.claim = replace(self.claim, lease_owner=worker_id)
        return self.claim

    def current_config(self, config_id):
        return self.config if config_id == self.config.id else None

    def complete_attempt(self, claim, result, now):
        self.attempts.append(result)
        cycle = claim.cycle_attempt_count + 1
        total = claim.attempt_count + 1
        self.claim = replace(
            claim,
            attempt_count=total,
            cycle_attempt_count=cycle,
            lease_owner="",
        )
        if result.delivered:
            self.status = "delivered"
            return
        delays = (5, 30, 300)
        if cycle <= len(delays):
            self.status = "retry_wait"
            self.next_at = now + timedelta(seconds=delays[cycle - 1])
        else:
            self.status = "failed"

    def release_lease(self, notification_id, worker_id):
        self.claim = replace(self.claim, lease_owner="")

    def cancel_missing_config(self, claim, now):
        self.status = "cancelled"

    def update_target(self, url):
        self.config = replace(
            self.config,
            draft=replace(self.config.draft, url=url),
        )


class AlarmHttpNotificationDispatcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_schedule_and_fourth_failure_is_terminal(self) -> None:
        module = _module()
        repository = _DeliveryRepository([500, 500, 500, 500])

        async def sender(request):
            status = repository.results.pop(0)
            return module.HttpSendResult(
                False,
                "rejected",
                status,
                1,
                "HTTP_NOTIFICATION_DELIVERY_REJECTED",
                "Remote endpoint rejected the request",
                None,
            )

        dispatcher = module.AlarmHttpNotificationDispatcher(
            repository,
            sender=sender,
            worker_id="worker-1",
        )
        expected = (
            (repository.now, "retry_wait", 1, repository.now + timedelta(seconds=5)),
            (repository.now + timedelta(seconds=5), "retry_wait", 2, repository.now + timedelta(seconds=35)),
            (repository.now + timedelta(seconds=35), "retry_wait", 3, repository.now + timedelta(seconds=335)),
            (repository.now + timedelta(seconds=335), "failed", 4, repository.now + timedelta(seconds=335)),
        )
        for moment, status, cycle, next_at in expected:
            self.assertEqual(1, await dispatcher.run_once(moment))
            self.assertEqual(status, repository.status)
            self.assertEqual(cycle, repository.claim.cycle_attempt_count)
            if status != "failed":
                self.assertEqual(next_at, repository.next_at)
        self.assertEqual(4, len(repository.attempts))

    async def test_retry_reads_the_current_request_configuration(self) -> None:
        module = _module()
        repository = _DeliveryRepository([500, 204])
        targets = []

        async def sender(request):
            targets.append(request.url)
            status = repository.results.pop(0)
            return module.HttpSendResult(
                status == 204,
                "delivered" if status == 204 else "rejected",
                status,
                1,
                None if status == 204 else "HTTP_NOTIFICATION_DELIVERY_REJECTED",
                None,
                None,
            )

        dispatcher = module.AlarmHttpNotificationDispatcher(
            repository,
            sender=sender,
            worker_id="worker-1",
        )
        await dispatcher.run_once(repository.now)
        repository.update_target("https://new.invalid/hook")
        await dispatcher.run_once(repository.now + timedelta(seconds=5))

        self.assertEqual(
            ["https://old.invalid/hook", "https://new.invalid/hook"],
            targets,
        )
        self.assertEqual("delivered", repository.status)

    async def test_disabled_configuration_waits_without_an_attempt(self) -> None:
        module = _module()
        repository = _DeliveryRepository([])
        repository.config = replace(repository.config, enabled=False)
        dispatcher = module.AlarmHttpNotificationDispatcher(
            repository,
            worker_id="worker-1",
        )

        self.assertEqual(0, await dispatcher.run_once(repository.now))
        self.assertEqual(0, repository.claim.attempt_count)
        self.assertEqual([], repository.attempts)


if __name__ == "__main__":
    unittest.main()
