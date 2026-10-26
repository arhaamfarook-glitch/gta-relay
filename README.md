Internet multiplayer without a VPN
=================================

Status: the adapter and relay pass local integration tests. No public endpoint is
configured. Cross-network gameplay and Mac execution have not been tested.

Players use the existing game helper, choose Connection: Internet, host a world,
and send its ONLINE-... invite code. A guest enters that code in Join LAN World.
There are no new player-side dependencies or VPN installations. LAN discovery
and existing GTA-... codes keep using the local-network transport.

Deployment (operator only)
--------------------------

Deploy Python 3.10+ and these files together:
  online_relay_server.py
  lan_session.py
  local_lan_session_info.py

Run: python3 online_relay_server.py --port 8790

The service listens only on 127.0.0.1. Put a managed HTTPS reverse proxy in front:

  multiplayer.YOUR-DOMAIN.example {
      request_body {
          max_size 384KB
      }
      reverse_proxy 127.0.0.1:8790
  }

Replace the example domain with an owned domain pointed at the host. Caddy
documentation: https://caddyserver.com/docs/quick-starts/https
Use a service manager to keep the relay running; configure normal operator-level
request rate limits and monitoring at the proxy. Do not expose port 8790 directly.
The relay is intended for small private friend groups, not a public game service.

Set DEFAULT_RELAY_URL in online_client.py to the deployed HTTPS address and build
the release/update normally. Players do not enter server configuration. For
operator testing only, GTA_RELAY_URL or multiplayer-config.json beside GTA-V.html
can override it with {"relay_url":"https://YOUR-DEPLOYED-HOST"}.

The updater already replaces the online_client.py bootstrap module. Once the
endpoint exists, include it in that module and regenerate the small update ZIP;
the bulk game data and local saves do not need replacing.

Transport and limits
--------------------

Authenticated HTTPS carries the existing bounded game-packet batches and native
session metadata. Room IDs are 80-bit random invite codes; connection tokens are
separate 256-bit random values. Redirects are refused so credentials are not
forwarded to another host. Plain HTTP is permitted only by explicit loopback test
fixtures. Save files and game assets never go to the relay. Usernames and packet
contents exist in relay memory while a room is active; this is transport TLS,
not end-to-end encryption against the relay operator.

The relay caps rooms, HTTP workers, body sizes and packet queues, limits room
creation/join attempts, and expires missing helpers after 25 seconds. Closing the
host closes the guests' session. Graceful shutdown uses the existing profile-save
acknowledgement handshake. The existing browser helper lease remains 20 seconds.

The Python standard-library HTTP server is a backend behind the HTTPS proxy,
not the public TLS terminator. Load, latency, packet loss, real gameplay and
cross-platform operation still need acceptance testing before a final release.
