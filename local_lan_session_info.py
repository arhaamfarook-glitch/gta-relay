"""Bounded envelope for an actual native rlSessionInfo::Export result.

This codec never creates native session data and does not mark a session ready.
The caller must authorize publication as the room's host and must obtain these
bytes from a successfully established native session on the game update thread.
Received bytes still require native Import, exact consumed length, IsValid, and
the normal JoinSession checks before use.
"""
from dataclasses import dataclass
import base64
import binascii
import re

VERSION = 1
MAX_NATIVE_BYTES = 91  # Export14573 asserts bytesWritten < 92.
MAX_BASE64_CHARS = 124
_ROOM = re.compile(r'^[0-9a-f]{24}$')
_KEYS = {'type', 'version', 'room', 'epoch', 'data'}


class SessionInfoError(ValueError):
    pass


def _identity(room, epoch):
    if not isinstance(room, str) or not _ROOM.fullmatch(room):
        raise SessionInfoError('Invalid LAN session room.')
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 1 <= epoch <= 0x7fffffff:
        raise SessionInfoError('Invalid LAN native session epoch.')


def export_message(room, epoch, native_bytes):
    """Wrap bytes already returned by the real native Export call."""
    _identity(room, epoch)
    if not isinstance(native_bytes, (bytes, bytearray, memoryview)):
        raise SessionInfoError('Native session export must be bytes.')
    native_bytes = bytes(native_bytes)
    if not 1 <= len(native_bytes) <= MAX_NATIVE_BYTES:
        raise SessionInfoError('Invalid native session export length.')
    return {'type': 'native-session-info', 'version': VERSION, 'room': room,
            'epoch': epoch, 'data': base64.b64encode(native_bytes).decode('ascii')}


@dataclass(frozen=True)
class NativeSessionExport:
    room: str
    epoch: int
    native_bytes: bytes


def import_message(message, expected_room, *, newer_than=None):
    """Validate the relay envelope; this is not native Import or readiness."""
    if not isinstance(message, dict) or set(message) != _KEYS:
        raise SessionInfoError('Invalid LAN session-info message.')
    if message['type'] != 'native-session-info' or type(message['version']) is not int or message['version'] != VERSION:
        raise SessionInfoError('Unsupported LAN session-info message.')
    room, epoch = message['room'], message['epoch']
    _identity(room, epoch)
    if room != expected_room:
        raise SessionInfoError('Native session belongs to another LAN room.')
    if newer_than is not None:
        if type(newer_than) is not int or not 0 <= newer_than <= 0x7fffffff:
            raise SessionInfoError('Invalid prior LAN session epoch.')
        if epoch <= newer_than:
            raise SessionInfoError('Stale native session export.')
    encoded = message['data']
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_BASE64_CHARS:
        raise SessionInfoError('Invalid encoded native session export.')
    try:
        native_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SessionInfoError('Invalid encoded native session export.') from error
    if not 1 <= len(native_bytes) <= MAX_NATIVE_BYTES or base64.b64encode(native_bytes).decode('ascii') != encoded:
        raise SessionInfoError('Invalid encoded native session export.')
    return NativeSessionExport(room, epoch, native_bytes)
