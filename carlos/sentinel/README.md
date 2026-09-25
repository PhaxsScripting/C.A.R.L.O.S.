# Carlos Sentinel reference node

This code is **not deployed**. The laptop cannot run it while off. It requires a separate always-on Linux node on the Ethernet broadcast network, with Python 3 and aiohttp. A router capable of sending authenticated wake requests could serve the same role.

The service binds loopback only. Expose it privately through that node's Tailscale Serve HTTPS; never use Funnel or a public port forward. Give every approved client a distinct 32-byte random key and explicit `status` / `wake` capabilities in a mode-0600 JSON config. Provision keys locally over an authenticated channel, never in chat. Keys are represented as 64 hex characters. Example schema (not usable credentials):

```json
{"target_mac":"00:00:00:00:00:00","broadcast":"192.168.1.255","devices":{"owner-phone":{"key":"REPLACE_LOCALLY","capabilities":["status","wake"],"revoked":false}}}
```

Run `python server.py --config /private/path/node.json` on the independent node. No root required. Use a dedicated service account, deny unrelated inbound traffic, and let its service manager restart bounded failures. The owner must set the actual fixed laptop Ethernet MAC and LAN broadcast. Packets are signed canonical JSON with version, device, timestamp, random nonce and action. Requests expire after 30 seconds; SQLite replay records persist across restarts. Per-client wakes are limited to one per 10 seconds. Revocation is read on every request. The protocol has no shell, arbitrary destination, shutdown, firmware, or general-command facility.

A response proves only that a packet was sent. Actual sleep/off wake must be tested with AC power, cable, configured firmware and a separate node. A network/clock error fails closed. Validate these requirements on your own hardware. The reference has no universal hardware wake guarantee.

Future microphones require a hardware mute switch, an obvious listening light, local wake processing and transient audio only. This reference deliberately does not pretend absent audio hardware exists.
