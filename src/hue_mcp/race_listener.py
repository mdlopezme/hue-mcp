"""A race kart's health and race-control flags, for hue-mcp's race light show.

Run in the kart stack's ROS 2 environment (its workspace sourced), with the Python ROS uses:

    python3 race_listener.py

Each message becomes one JSON line on stdout: {"stop": <stop type>} from the fault
multiplexer's status, {"flags": <bitfield>} from race control. {"ready": true} comes first, once
both subscriptions exist. It never imports hue_mcp, and it must keep working on ROS's Python,
outside any virtual environment.
"""

import json
import sys

# Shared with hue_mcp.race, and kept equal by a test: this script can't import the package.
EXIT_MISSING = 6  # rclpy or race_msgs isn't importable: the workspace isn't built or sourced.
FAULTS_TOPIC = "/faults/status"
FLAGS_TOPIC = "/rc_to_ct"

try:
    import rclpy
    from race_msgs.msg import FaultMultiplexerStatus, VehicleCommand
    from rclpy.executors import ExternalShutdownException
    from rclpy.qos import qos_profile_sensor_data
except ImportError:
    sys.exit(EXIT_MISSING)


def say(message: dict[str, object]) -> None:
    print(json.dumps(message), flush=True)


def main() -> None:
    rclpy.init()
    node = rclpy.create_node("hue_mcp_race_listener")
    # Best effort matches publishers of either reliability.
    node.create_subscription(
        FaultMultiplexerStatus,
        FAULTS_TOPIC,
        lambda status: say({"stop": status.stop_type.stop_type}),
        qos_profile_sensor_data,
    )
    node.create_subscription(
        VehicleCommand,
        FLAGS_TOPIC,
        lambda command: say({"flags": command.flags}),
        qos_profile_sensor_data,
    )
    say({"ready": True})
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        return  # Ctrl+C or SIGTERM: a normal stop.


if __name__ == "__main__":
    main()
