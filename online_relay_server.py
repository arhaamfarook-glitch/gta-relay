"""Game packet relay. Deploy behind HTTPS; never serves game files or saves."""
import argparse
import base64
from collections import deque
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from lan_session import LanRoom, LanClient, LanError, username


class OnlineRelay:
    def __init__(self, max_rooms=16, lease=25):
        self.lock = threading.RLock()
        self.public_code = None
        self.rooms = {}
        self.clients = {}
        self.creates = {}
        self.max_rooms = max_rooms
        self.lease = lease

    def expire(self):
        now = time.monotonic()
        with self.lock:
            stale = [token for token, row in self.clients.items() if now - row['seen'] > self.lease]
            for token in stale:
                self._leave(token)
            for code, room in list(self.rooms.items()):
                if room.shared and not room.peers:
                    room.close()
                if room.closed.is_set():
                    del self.rooms[code]
            self.creates = {ip: times for ip, times in self.creates.items() if times and now - times[-1] < 60}

    def _leave(self, token):
        row = self.clients.pop(token, None)
        if row:
            row['client'].close()
            # Explicitly close the room before returning; guests cannot keep an
            # orphaned host session alive by continuing to poll it.
            if row['client'].welcome['host'] and not row['room'].shared:
                row['room'].close('The host disconnected from the online world.')

    def call(self, action, data, token='', address='127.0.0.1'):
        if action=='public-status':
            with self.lock:
                room=self.rooms.get(self.public_code)
                snapshot=room.snapshot() if room and not room.closed.is_set() else {}
                return {'players':len(snapshot.get('players',[])),'max_players':4,'ready':snapshot.get('native_ready',False),'available':True}
        if action=='public-join':
            with self.lock:
                self.expire()
                room=self.rooms.get(self.public_code)
                if room is None or room.closed.is_set():
                    result=self.call('host',{**data,'shared':True},address=address)
                    self.public_code=result['code']
                else:
                    result=self.call('join',{**data,'code':self.public_code},address=address)
                result['public_world']=True
                return result
        if action in ('host', 'join'):
            name = username(data.get('username'))
            with self.lock:
                self.expire()
                times = self.creates.setdefault(address, deque(maxlen=12))
                now = time.monotonic()
                if len(times) == 12 and now - times[0] < 60:
                    raise LanError('Too many connection attempts. Wait a minute and try again.')
                times.append(now)
                if len(self.creates) > 4096:
                    raise LanError('The online service is busy. Try again later.')
                if action == 'host':
                    if len(self.rooms) >= self.max_rooms:
                        raise LanError('The online relay is full. Try again later.')
                    code = 'ONLINE-' + base64.b32encode(secrets.token_bytes(10)).decode()
                    room = LanRoom('127.0.0.1', shared=data.get('shared') is True)
                else:
                    code = data.get('code', '')
                    if not isinstance(code, str) or not re.fullmatch(r'ONLINE-[A-Z2-7]{16}', code):
                        raise LanError('Enter the online join code from your host.')
                    room = self.rooms.get(code)
                    if room is None or room.closed.is_set():
                        raise LanError('This online world has ended or its code is incorrect.')
                try:
                    client = LanClient(room.code, name, preferred_model=data.get('model'),
                                       host_secret=room.host_secret if action == 'host' else None)
                except Exception:
                    if action == 'host':
                        room.close()
                    raise
                if action == 'host':
                    self.rooms[code] = room
                token = secrets.token_urlsafe(32)
                self.clients[token] = {'client': client, 'room': room, 'seen': now, 'poll': threading.Lock()}
                return {'token': token, 'code': code, 'player': client.welcome}
        with self.lock:
            row = self.clients.get(token)
            if row is None:
                raise LanError('This online connection has ended.')
            row['seen'] = time.monotonic()
            client, room = row['client'], row['room']
        if action == 'send':
            packets = data.get('packets')
            if not isinstance(packets, list) or not 1 <= len(packets) <= 64:
                raise LanError('Invalid online packet batch.')
            decoded = []
            for packet in packets:
                if not isinstance(packet, str) or len(packet) > 5500:
                    raise LanError('Invalid online packet.')
                body = base64.b64decode(packet, validate=True)
                if not body or len(body) > 4105 or body[:1] not in (b'J', b'D'):
                    raise LanError('Invalid online packet.')
                decoded.append(body)
            for body in decoded:
                client.send(body)
            return {'sent': len(decoded)}
        if action == 'claim-coordinator':
            if not room.shared:raise LanError('This is not the shared Online server.')
            with room.lock:
                peer=room.peers.get(client.welcome['id'])
                if peer is None:raise LanError('This player has left the server.')
                if not peer.coordinator:
                    for candidate in room.peers.values():candidate.coordinator=candidate is peer
                    room.native_ready=False
                    room.native_session_info=None
                epoch=room.native_session_epoch+1
            room.broadcast(b'J'+json.dumps(room.snapshot(),separators=(',',':')).encode())
            return {'epoch':epoch}
        if action == 'receive':
            if not row['poll'].acquire(blocking=False):
                raise LanError('Another receiver is reading this online world.')
            try:
                packets = client.receive(.4)
            finally:
                row['poll'].release()
            return {'packets': [base64.b64encode(p).decode() for p in packets],
                    'ended': client.closed or room.closed.is_set()}
        if action in ('end', 'begin-end'):
            if room.shared:raise LanError('Leave Online to return to Story Mode. Other players stay connected.')
            if not client.welcome['host']:
                raise LanError('Only the host can end this world.')
            epoch = room.begin_end()
            if action == 'begin-end':
                return {'epoch': epoch}
            room.closed.wait(10)
            return {'ended': room.closed.is_set(), 'epoch': epoch, 'profiles': room.end_result}
        if action == 'leave':
            with self.lock:
                self._leave(token)
            return {'ended': True}
        raise LanError('Unknown online action.')

    def close(self):
        with self.lock:
            for token in list(self.clients):
                self._leave(token)
            for room in self.rooms.values():
                room.close()
            self.rooms.clear()


def make_server(relay, address=('127.0.0.1', 8790)):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def do_GET(self):
            body = b'{"service":"gta-online-relay","protocol":1,"shared_world":true}'
            self.send_response(200 if self.path == '/health' else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            status = 200
            try:
                if self.headers.get('Origin'):
                    raise LanError('Connect using the game helper.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 384 * 1024 or self.headers.get('Transfer-Encoding'):
                    raise LanError('Invalid request size.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or not self.path.startswith('/v1/'):
                    raise LanError('Invalid request.')
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                result = relay.call(self.path[4:], data, token, self.client_address[0])
            except (LanError, OSError, ValueError, TypeError) as error:
                status, result = 400, {'error': str(error)}
            body = json.dumps(result, separators=(',', ':')).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass  # Do not log invite codes, usernames or authentication tokens.

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(96)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

        def service_actions(self):
            relay.expire()
    return Server(address, Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    relay = OnlineRelay()
    server = make_server(relay, (args.host, args.port))
    try:
        server.serve_forever()
    finally:
        relay.close()
        server.server_close()
