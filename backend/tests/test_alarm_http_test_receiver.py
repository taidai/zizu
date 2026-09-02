from __future__ import annotations

import http.client
import json
import unittest

from scripts.alarm_http_test_receiver import AlarmHttpTestReceiver


class AlarmHttpTestReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.receiver = AlarmHttpTestReceiver("127.0.0.1", 0)
        self.receiver.start()

    def tearDown(self) -> None:
        self.receiver.stop()

    def _post(self, key: str) -> int:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.receiver.port,
            timeout=2,
        )
        connection.request(
            "POST",
            "/hook",
            body=json.dumps({"type": "ALARM_ACTIVATED"}),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def test_receiver_deduplicates_by_idempotency_key(self) -> None:
        self.assertEqual(204, self._post("same-id"))
        self.assertEqual(204, self._post("same-id"))
        self.assertEqual(1, len(self.receiver.records()))

    def test_records_endpoint_is_json_and_delete_clears_records(self) -> None:
        self._post("one")
        connection = http.client.HTTPConnection("127.0.0.1", self.receiver.port, timeout=2)
        connection.request("GET", "/records")
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual("one", payload["items"][0]["idempotency_key"])
        self.assertEqual("ALARM_ACTIVATED", payload["items"][0]["body"]["type"])
        connection.request("DELETE", "/records")
        cleared = connection.getresponse()
        cleared.read()
        connection.close()
        self.assertEqual(204, cleared.status)
        self.assertEqual([], self.receiver.records())


if __name__ == "__main__":
    unittest.main()
