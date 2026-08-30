import io
import unittest
import urllib.error
from unittest.mock import patch

from app.services.neuron_client import NeuronClient, NeuronConfig


class NeuronClientTokenRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = NeuronClient(
            NeuronConfig(
                password="not-an-example-secret",
                deployment_mode="production",
            )
        )

    @staticmethod
    def forbidden() -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "http://127.0.0.1:7000/api/v2/node?type=1",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error":"expired token"}'),
        )

    def test_reauthenticates_once_when_neuron_rejects_a_stale_cached_token(self) -> None:
        self.client._token = "stale-token"
        with patch.object(
            self.client,
            "_http_request",
            side_effect=[
                self.forbidden(),
                {"token": "fresh-token"},
                {"nodes": [{"name": "en9_pcs"}]},
            ],
        ) as request:
            nodes = self.client.get_nodes()

        self.assertEqual([{"name": "en9_pcs"}], nodes)
        self.assertEqual(3, request.call_count)
        self.assertEqual("fresh-token", self.client._token)
        self.assertEqual(
            "Bearer fresh-token",
            request.call_args_list[2].kwargs["headers"]["Authorization"],
        )

    def test_does_not_retry_non_authentication_failures(self) -> None:
        self.client._token = "current-token"
        unavailable = urllib.error.HTTPError(
            "http://127.0.0.1:7000/api/v2/node?type=1",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"{}"),
        )
        with patch.object(self.client, "_http_request", side_effect=unavailable) as request:
            with self.assertRaises(urllib.error.HTTPError):
                self.client.get_nodes()

        self.assertEqual(1, request.call_count)
        self.assertEqual("current-token", self.client._token)


if __name__ == "__main__":
    unittest.main()
