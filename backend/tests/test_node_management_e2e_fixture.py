import unittest
from unittest.mock import MagicMock, patch

from scripts.node_management_e2e_fixture import (
    build_neuron_tags,
    build_resource_names,
    build_telemetry_payload,
    publish,
    setup,
)


class NodeManagementE2EFixtureTest(unittest.TestCase):
    def test_resource_names_are_deterministic_and_namespaced(self) -> None:
        names = build_resource_names("E2E验证", "20260830T120000Z")

        self.assertEqual("E2E验证-设备-20260830T120000Z", names.platform_node)
        self.assertEqual("zizu_e2e_20260830T120000Z", names.neuron_node)
        self.assertEqual("e2e_data", names.neuron_group)
        self.assertEqual("e2e_active_power", names.neuron_tag)

    def test_resource_names_reject_non_e2e_root_and_unsafe_run_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "E2E验证"):
            build_resource_names("储能", "20260830T120000Z")
        with self.assertRaisesRegex(ValueError, "run id"):
            build_resource_names("E2E验证", "../escape")

    def test_neuron_resource_names_normalize_hyphens_rejected_by_neuron(self) -> None:
        names = build_resource_names("E2E验证", "run-20260830-1")

        self.assertEqual("E2E验证-设备-run-20260830-1", names.platform_node)
        self.assertEqual("zizu_e2e_run_20260830_1", names.neuron_node)
        self.assertEqual("e2e_data", names.neuron_group)

    def test_telemetry_payload_matches_the_isolated_neuron_source(self) -> None:
        names = build_resource_names("E2E验证", "20260830T120000Z")
        topic, payload = build_telemetry_payload(
            names,
            value=12.5,
            timestamp_ms=1_777_777_777_000,
        )

        self.assertEqual("/neuron/zizu_e2e_20260830T120000Z/telemetry", topic)
        self.assertEqual(
            {
                "node_name": names.neuron_node,
                "group": names.neuron_group,
                "timestamp": 1_777_777_777_000,
                "tags": {"e2e_active_power": 12.5},
            },
            payload,
        )

    def test_neuron_catalog_has_two_pages_without_touching_real_points(self) -> None:
        names = build_resource_names("E2E验证", "20260830T120000Z")
        tags = build_neuron_tags(names)

        self.assertEqual(51, len(tags))
        self.assertEqual(
            {"name": names.neuron_tag, "address": "1!400001", "attribute": 1, "type": 4},
            tags[0],
        )
        self.assertEqual("e2e_spare_050", tags[-1]["name"])
        self.assertEqual(51, len({tag["address"] for tag in tags}))

    @patch.dict(
        "os.environ",
        {
            "ZIZU_E2E_WRITE_ROOT": "E2E验证",
            "ZIZU_E2E_RUN_ID": "20260830T120000Z",
        },
        clear=True,
    )
    @patch("scripts.node_management_e2e_fixture._setup_neuron_via_ssh")
    @patch("scripts.node_management_e2e_fixture._request")
    @patch("scripts.node_management_e2e_fixture._token", return_value="token")
    def test_setup_repairs_existing_partial_fixture(
        self,
        _token: MagicMock,
        request: MagicMock,
        setup_via_ssh: MagicMock,
    ) -> None:
        request.return_value = {
            "nodes": [{"name": "zizu_e2e_20260830T120000Z"}]
        }

        setup()

        setup_via_ssh.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "ZIZU_E2E_BASE_URL": "http://e606.hlszh.com:9000",
            "ZIZU_E2E_WRITE_ROOT": "E2E验证",
            "ZIZU_E2E_RUN_ID": "20260830T120000Z",
        },
        clear=True,
    )
    @patch("scripts.node_management_e2e_fixture._publish_via_ssh")
    @patch("scripts.node_management_e2e_fixture.mqtt.Client")
    def test_publish_falls_back_to_the_pinned_ssh_host_when_mqtt_is_private(
        self,
        mqtt_client: MagicMock,
        publish_via_ssh: MagicMock,
    ) -> None:
        mqtt_client.return_value.connect.side_effect = ConnectionRefusedError("private broker")

        result = publish(15.25)

        self.assertEqual("published", result["status"])
        self.assertEqual("ssh", result["transport"])
        publish_via_ssh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
