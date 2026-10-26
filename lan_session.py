"""Private, temporary LAN relay for the browser engine's UDP transport.

No game saves, assets, or filesystem endpoints are exposed by this listener.
The normal game HTTP server remains bound to loopback.
"""
import base64
import ipaddress
import json
import math
import secrets
import socket
import struct
import threading
import time
import unicodedata
from collections import deque
from local_lan_session_info import export_message, import_message

VERSION = 1
MAX_FRAME = 65536
MAX_PLAYERS = 4
MAX_QUEUED_PACKETS = 2048
MAX_QUEUED_BYTES = 4 * 1024 * 1024
MODELS = (
    'a_m_y_business_01', 'a_f_y_business_01', 'a_m_y_skater_01',
    'a_m_y_hipster_01', 'a_f_y_hipster_01', 'a_m_m_business_01',
    'a_m_y_beach_01', 'a_f_y_tourist_01',
)


class LanError(Exception):
    pass


def username(value):
    if not isinstance(value, str):
        raise LanError('Enter a player name.')
    value = unicodedata.normalize('NFC', value).strip()
    if not 1 <= len(value) <= 24 or any(
        unicodedata.category(c).startswith('C') or c in '<>\\/' for c in value
    ):
        raise LanError('Use 1–24 letters, numbers, spaces or punctuation for your name.')
    return value


def player_roster(value, shared=False):
    """Copy the relay's small public roster without retaining message objects."""
    if not isinstance(value, list) or len(value) > MAX_PLAYERS:
        raise LanError('Invalid LAN player roster.')
    result, numbers, names, models = [], set(), set(), set()
    for player in value:
        if not isinstance(player, dict) or set(player) != {'id', 'username', 'model', 'address', 'host'}:
            raise LanError('Invalid LAN player roster.')
        number = player['id']
        if type(number) is not int or not 1 <= number <= MAX_PLAYERS or number in numbers:
            raise LanError('Invalid LAN player roster.')
        name = username(player['username'])
        if name != player['username'] or name.casefold() in names or \
                player['model'] not in MODELS or player['model'] in models or player['address'] != '10.77.0.' + str(number) or \
                type(player['host']) is not bool or (not shared and player['host'] != (number == 1)):
            raise LanError('Invalid LAN player roster.')
        numbers.add(number)
        names.add(name.casefold())
        models.add(player['model'])
        result.append({'id': number, 'username': name, 'model': player['model'],
                       'address': player['address'], 'host': player['host']})
    if shared and result and sum(p['host'] for p in result)!=1:raise LanError('Invalid shared-world coordinator.')
    return sorted(result, key=lambda player: player['id'])


def private_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise LanError('Invalid local network address.') from error
    if not (address.is_private or address.is_loopback or address in ipaddress.IPv4Network('100.64.0.0/10')) or address.is_multicast or address.is_unspecified:
        raise LanError('LAN sessions must use a local network address.')
    return str(address)


def local_address():
    # Connecting a UDP socket chooses the local interface without sending data.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))
        return private_ip(probe.getsockname()[0])
    except OSError as error:
        raise LanError('No local network connection is available.') from error
    finally:
        probe.close()


def make_code(address, port, secret):
    payload = socket.inet_aton(private_ip(address)) + struct.pack('!H', port) + secret
    encoded = base64.b32encode(payload).decode('ascii').rstrip('=')
    return 'GTA-' + '-'.join(encoded[i:i + 5] for i in range(0, len(encoded), 5))


def parse_code(code):
    if not isinstance(code, str) or len(code) > 80:
        raise LanError('Invalid LAN join code.')
    encoded = code.strip().upper().removeprefix('GTA-').replace('-', '').replace(' ', '')
    try:
        payload = base64.b32decode(encoded + '=' * (-len(encoded) % 8))
    except (ValueError, base64.binascii.Error) as error:
        raise LanError('Invalid LAN join code.') from error
    if len(payload) != 18:
        raise LanError('Invalid LAN join code.')
    address = private_ip(socket.inet_ntoa(payload[:4]))
    port = struct.unpack('!H', payload[4:6])[0]
    if not port:
        raise LanError('Invalid LAN join code.')
    return address, port, payload[6:]


def read_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise EOFError('LAN connection closed.')
        result.extend(block)
    return bytes(result)


def read_frame(connection):
    size = struct.unpack('!I', read_exact(connection, 4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise LanError('Invalid LAN frame.')
    return read_exact(connection, size)


def frame(payload):
    if not 1 <= len(payload) <= MAX_FRAME:
        raise LanError('Invalid LAN frame.')
    return struct.pack('!I', len(payload)) + payload


def json_frame(message):
    return b'J' + json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def decode_json(payload):
    if not payload.startswith(b'J') or len(payload) > 8192:
        raise LanError('Invalid LAN control message.')
    result = json.loads(payload[1:])
    if not isinstance(result, dict):
        raise LanError('Invalid LAN control message.')
    return result


class Peer:
    def __init__(self, number, name, connection, model=None):
        self.number = number
        self.coordinator = number == 1
        self.name = name
        self.model = model or MODELS[number - 1]
        self.address = '10.77.0.' + str(number)
        self.connection = connection
        self.lock = threading.Lock()
        self.ports = set()
        self.window = time.monotonic()
        self.bytes = 0

    def send(self, payload):
        with self.lock:
            self.connection.sendall(frame(payload))

    def public(self):
        return {'id': self.number, 'username': self.name, 'model': self.model,
                'address': self.address, 'host': self.coordinator}


class LanRoom:
    def __init__(self, address=None, port=0, max_players=MAX_PLAYERS, shared=False):
        self.shared = shared
        self.address = private_ip(address or local_address())
        self.limit = min(MAX_PLAYERS, max(2, max_players))
        self.secret = secrets.token_bytes(12)
        self.host_secret = secrets.token_bytes(12)
        self.id = secrets.token_hex(12)
        self.lock = threading.RLock()
        self.peers = {}
        self.host_spawn = None
        self.native_ready = False
        self.native_session_info = None
        self.native_session_epoch = 0
        self.ending_epoch = None
        self.ending_pending = set()
        self.ending_names = {}
        self.ending_ack = threading.Event()
        self.end_result = None
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.listener.bind((self.address, port))
        self.listener.listen(8)
        self.listener.settimeout(.5)
        self.port = self.listener.getsockname()[1]
        self.code = make_code(self.address, self.port, self.secret)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, address = self.listener.accept()
                private_ip(address[0])
                connection.settimeout(8)
                threading.Thread(target=self._serve, args=(connection,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break
            except LanError:
                connection.close()

    def snapshot(self):
        with self.lock:
            return {'type': 'players', 'room': self.id,
                    'players': player_roster([p.public() for p in self.peers.values()], shared=self.shared) if not self.closed.is_set() else [],
                    'spawn': list(self.host_spawn) if self.host_spawn is not None else None,
                    'native_ready': self.native_ready,
                    'native_session_info': dict(self.native_session_info) if self.native_session_info else None,
                    'persistent': False, 'missions': False,
                    'ending': self.ending_epoch}

    def begin_end(self,timeout=8):
        """Keep native transport alive while each player's localhost saves."""
        with self.lock:
            if self.closed.is_set():return self.ending_epoch
            if self.ending_epoch:return self.ending_epoch
            self.ending_epoch=secrets.token_hex(12)
            self.ending_pending=set(self.peers)
            self.ending_names={number:peer.name for number,peer in self.peers.items()}
            epoch=self.ending_epoch
        self.broadcast(json_frame({'type':'ending','epoch':epoch}))
        def finish():
            self.ending_ack.wait(timeout)
            with self.lock:
                self.end_result={'saved':[name for number,name in self.ending_names.items() if number not in self.ending_pending],
                                 'missing':[name for number,name in self.ending_names.items() if number in self.ending_pending]}
            self.close('The host ended this LAN world.')
        threading.Thread(target=finish,daemon=True).start()
        return epoch

    def broadcast(self, payload, exclude=None):
        with self.lock:
            peers = list(self.peers.values())
        for peer in peers:
            if peer.number == exclude:
                continue
            try:
                peer.send(payload)
            except OSError:
                pass

    def _serve(self, connection):
        peer = None
        try:
            hello = decode_json(read_frame(connection))
            token = bytes.fromhex(hello.get('token', ''))
            host = secrets.compare_digest(token, self.host_secret)
            if hello.get('version') != VERSION or not (host or secrets.compare_digest(token, self.secret)):
                raise LanError('The LAN code is incorrect or the session has ended.')
            name = username(hello.get('username'))
            preferred = hello.get('model')
            if preferred is not None and preferred not in MODELS:raise LanError('Invalid LAN character model.')
            with self.lock:
                if self.closed.is_set():
                    raise LanError('The LAN session has ended.')
                if self.ending_epoch:raise LanError('The host is saving and closing this LAN world.')
                if any(p.name.casefold() == name.casefold() for p in self.peers.values()):
                    raise LanError('That player name is already in this world.')
                if host and 1 in self.peers:
                    raise LanError('This world already has a host.')
                if not host and not any(p.coordinator for p in self.peers.values()):
                    raise LanError('The host world is not ready.')
                number = 1 if host else next((i for i in range(1 if self.shared else 2, self.limit + 1) if i not in self.peers), None)
                if number is None:
                    raise LanError('This LAN world is full.')
                if not host and (self.host_spawn is None or not self.native_ready):
                    raise LanError('The host world is still loading. Try joining once the host is playing.')
                used_models={p.model for p in self.peers.values()}
                model=next(model for model in (preferred,MODELS[number-1],*MODELS) if model in MODELS and model not in used_models)
                peer = Peer(number, name, connection,model)
                peer.coordinator = host
                self.peers[number] = peer
                peer.send(json_frame({'type': 'joined', 'room': self.id, **peer.public(),
                                      'players': player_roster([p.public() for p in self.peers.values()], shared=self.shared),
                                      'spawn': self.host_spawn, 'native_ready': self.native_ready,
                                      'native_session_info': self.native_session_info,
                                      'persistent': False, 'missions': False, 'shared_world': self.shared}))
            connection.settimeout(30)
            self.broadcast(json_frame(self.snapshot()))
            while not self.closed.is_set():
                payload = read_frame(connection)
                now = time.monotonic()
                if now - peer.window >= 1:
                    peer.window, peer.bytes = now, 0
                peer.bytes += len(payload)
                if peer.bytes > 4 * 1024 * 1024:
                    raise LanError('LAN packet limit exceeded.')
                if payload[0] == ord('J'):
                    self._control(peer, decode_json(payload))
                elif payload[0] == ord('D'):
                    self._datagram(peer, payload)
                else:
                    raise LanError('Invalid LAN packet.')
        except (OSError, EOFError, ValueError, TypeError, LanError) as error:
            try:
                (peer.send if peer else lambda p: connection.sendall(frame(p)))(json_frame({'type': 'error', 'message': str(error)}))
            except OSError:
                pass
        finally:
            with self.lock:
                if peer:
                    self.peers.pop(peer.number, None)
                    if self.shared and peer.coordinator and self.peers:
                        self.peers[min(self.peers)].coordinator = True
                        self.native_ready = False
                        self.native_session_info = None
            try:
                connection.close()
            except OSError:
                pass
            if peer and self.shared:
                with self.lock:
                    if peer.coordinator and self.peers:
                        self.peers[min(self.peers)].coordinator=True
                        self.native_ready=False
                        self.native_session_info=None
                if not self.closed.is_set():self.broadcast(json_frame(self.snapshot()))
            elif peer and peer.coordinator:
                self.close('The host ended the LAN world.')
            elif peer and not self.closed.is_set():
                self.broadcast(json_frame(self.snapshot()))

    def _control(self, peer, message):
        action = message.get('type')
        if action == 'ping':
            peer.send(json_frame({'type': 'pong'}))
        elif action == 'bind':
            port = message.get('port')
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or (port not in peer.ports and len(peer.ports) >= 16):
                raise LanError('Invalid LAN socket.')
            peer.ports.add(port)
            peer.send(json_frame({'type': 'bound', 'port': port}))
        elif action == 'unbind':
            peer.ports.discard(message.get('port'))
        elif action == 'spawn' and peer.coordinator:
            position = message.get('position')
            if not isinstance(position, list) or len(position) != 4 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 100000 for v in position
            ):
                raise LanError('Invalid host spawn position.')
            with self.lock:
                self.host_spawn = list(position)
            self.broadcast(json_frame({'type': 'spawn', 'position': self.host_spawn}))
        elif action == 'native-ready' and peer.coordinator:
            ready = message.get('ready')
            if not isinstance(ready, bool):
                raise LanError('Invalid native session state.')
            with self.lock:
                self.native_ready = ready
                if not ready:
                    self.native_session_info = None
            self.broadcast(json_frame(self.snapshot()))
        elif action == 'native-session-info' and peer.coordinator:
            with self.lock:
                decoded = import_message(message, self.id, newer_than=self.native_session_epoch)
                # Store only a canonical, bounded copy. An export is connection
                # metadata, not evidence that gameplay or native hosting works.
                self.native_session_info = export_message(self.id, decoded.epoch, decoded.native_bytes)
                self.native_session_epoch = decoded.epoch
                publication = dict(self.native_session_info)
            self.broadcast(json_frame(publication))
        elif action == 'profile-saved':
            with self.lock:
                if not self.ending_epoch or message.get('epoch')!=self.ending_epoch:raise LanError('Invalid LAN profile save acknowledgement.')
                self.ending_pending.discard(peer.number)
                if not self.ending_pending:self.ending_ack.set()
        elif action == 'end' and peer.coordinator:
            if self.shared:
                raise LanError('Players can leave the shared world, not end it.')
            self.close('The host ended the LAN world.')
        else:
            raise LanError('Invalid LAN command.')

    def _datagram(self, peer, payload):
        # D + source port + target port + target IPv4 + native packet bytes.
        if len(payload) < 9:
            raise LanError('Invalid LAN datagram.')
        source, target = struct.unpack('!HH', payload[1:5])
        if source not in peer.ports or not target:
            raise LanError('LAN socket is not bound.')
        address = socket.inet_ntoa(payload[5:9])
        broadcast = address in ('255.255.255.255', '10.77.0.255')
        if not broadcast and not address.startswith('10.77.0.'):
            raise LanError('Packets cannot leave this LAN world.')
        with self.lock:
            targets = [p for p in self.peers.values() if target in p.ports and
                       ((broadcast and p.number != peer.number) or (not broadcast and p.address == address))]
        incoming = b'D' + struct.pack('!HH', source, target) + socket.inet_aton(peer.address) + payload[9:]
        for other in targets:
            other.send(incoming)

    def close(self, reason='The LAN world has ended.'):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            self.ending_ack.set()
            peers = list(self.peers.values())
            self.host_spawn = None
            self.native_ready = False
            self.native_session_info = None
            self.native_session_epoch = 0
        for peer in peers:
            try:
                peer.send(json_frame({'type': 'ended', 'message': reason}))
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()


class LanClient:
    """A local helper connection; browser/native adapters consume its packet queue."""
    def __init__(self, code, name, host_secret=None, preferred_model=None):
        name = username(name)
        address, port, secret = parse_code(code)
        self.connection = socket.create_connection((address, port), timeout=5)
        self.connection.settimeout(10)
        self.send_lock = threading.Lock()
        self.condition = threading.Condition()
        self.messages = deque()
        self.queued_bytes = 0
        self.closed = False
        self.state = {'room': None, 'players': [], 'spawn': None, 'native_ready': False, 'native_session_info': None,'ending':None}
        self.send(json_frame({'version': VERSION, 'token': (host_secret or secret).hex(), 'username': name,
                              **({'model':preferred_model} if preferred_model else {})}))
        try:
            self.welcome = decode_json(read_frame(self.connection))
            if self.welcome.get('type') != 'joined':
                raise LanError(self.welcome.get('message', 'Could not join this LAN world.'))
            self.state['room'] = self.welcome['room']
            self._remember(self.welcome)
        except Exception:
            self.connection.close()
            raise
        self.connection.settimeout(30)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
        self.heartbeat.start()

    def send(self, payload):
        with self.send_lock:
            self.connection.sendall(frame(payload))

    def control(self, **message):
        self.send(json_frame(message))

    def datagram(self, source, target, address, data):
        if len(data) > MAX_FRAME - 9:
            raise LanError('LAN datagram is too large.')
        self.send(b'D' + struct.pack('!HH', source, target) + socket.inet_aton(address) + data)

    def _remember(self, message):
        """Keep control state independently of the renderer's packet consumer."""
        action = message.get('type')
        if action in ('joined', 'players'):
            self.state['players'] = player_roster(message.get('players'), shared=self.welcome.get('shared_world',False))
            current=next((p for p in self.state['players'] if p['id']==self.welcome['id']),None)
            if current:self.welcome['host']=current['host']
            self.state['spawn'] = message.get('spawn')
            self.state['native_ready'] = message.get('native_ready') is True
            publication = message.get('native_session_info')
            self.state['native_session_info'] = None
            if publication is not None:
                decoded = import_message(publication, self.state['room'])
                self.state['native_session_info'] = export_message(decoded.room, decoded.epoch, decoded.native_bytes)
        elif action == 'spawn':
            self.state['spawn'] = message.get('position')
        elif action == 'native-session-info':
            prior = self.state['native_session_info']
            decoded = import_message(message, self.state['room'], newer_than=prior['epoch'] if prior else 0)
            self.state['native_session_info'] = export_message(decoded.room, decoded.epoch, decoded.native_bytes)
        elif action == 'ending':
            epoch=message.get('epoch')
            if not isinstance(epoch,str) or len(epoch)!=24 or any(c not in '0123456789abcdef' for c in epoch):raise LanError('Invalid LAN shutdown request.')
            self.state['ending']=epoch
        elif action in ('ended', 'error'):
            self.state.update(players=[], spawn=None, native_ready=False, native_session_info=None)

    def snapshot(self):
        with self.condition:
            return {'room': self.state['room'],
                    'players': [dict(player) for player in self.state['players']],
                    'spawn': list(self.state['spawn']) if self.state['spawn'] is not None else None,
                    'native_ready': self.state['native_ready'],
                    'native_session_info': dict(self.state['native_session_info']) if self.state['native_session_info'] else None,
                    'ending':self.state['ending']}

    def _read(self):
        try:
            while not self.closed:
                payload = read_frame(self.connection)
                with self.condition:
                    message = decode_json(payload) if payload.startswith(b'J') else None
                    if message is not None:
                        self._remember(message)
                    # Datagrams are lossy by design; bound memory for a stalled renderer.
                    while self.messages and (self.queued_bytes + len(payload) > MAX_QUEUED_BYTES or len(self.messages) >= MAX_QUEUED_PACKETS):
                        old = self.messages.popleft()
                        self.queued_bytes -= len(old)
                    self.messages.append(payload)
                    self.queued_bytes += len(payload)
                    self.condition.notify_all()
                if message is not None and message.get('type') in ('ended', 'error'):
                    break
        except (OSError, EOFError, LanError, ValueError):
            pass
        finally:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            with self.condition:
                self.closed = True
                self.state.update(players=[], spawn=None, native_ready=False, native_session_info=None)
                self.condition.notify_all()

    def _heartbeat(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed, timeout=5)
                if self.closed:
                    return
            try:
                self.control(type='ping')
            except OSError:
                self.close()
                return

    def receive(self, timeout=1):
        with self.condition:
            self.condition.wait_for(lambda: self.messages or self.closed, timeout=timeout)
            messages = list(self.messages)
            self.messages.clear()
            self.queued_bytes = 0
            return messages

    def close(self):
        with self.condition:
            self.messages.clear()
            self.queued_bytes = 0
            self.closed = True
            self.state.update(players=[], spawn=None, native_ready=False, native_session_info=None)
            self.condition.notify_all()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

