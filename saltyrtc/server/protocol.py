import asyncio
import os
import struct

import libnacl
import libnacl.public
import websockets

from . import util
from .common import (
    COOKIE_LENGTH,
    KEEP_ALIVE_INTERVAL_DEFAULT,
    KEEP_ALIVE_INTERVAL_MIN,
    KEEP_ALIVE_TIMEOUT,
    KEY_LENGTH,
    AddressType,
    OverflowSentinel,
    available_slot_range,
    is_initiator_id,
    is_responder_id,
)
from .exception import (
    Disconnected,
    InternalError,
    MessageError,
    MessageFlowError,
    SlotsFullError,
)
from .message import unpack

__all__ = (
    'Path',
    'PathClient',
    'Protocol',
)


class Path:
    __slots__ = ('_slots', 'log', 'initiator_key', 'number')

    def __init__(self, initiator_key, number):
        self._slots = {id_: None for id_ in available_slot_range()}
        self.log = util.get_logger('path.{}'.format(number))
        self.initiator_key = initiator_key
        self.number = number

    @property
    def empty(self):
        """
        Return `True` in case the path is empty. A call to this property
        will also remove clients from the path whose connections are
        closed but have not been removed from the path. (However, in
        case that the path is not empty, this property does not ensure
        that all disconnected clients will be removed.)
        """
        for client in self._slots.values():
            if client is not None:
                if client.connection_closed.done():
                    self.remove_client(client)
                    self.log.notice('Removed dead client {}', client)
                else:
                    return False
        return True

    def get_initiator(self):
        """
        Return the initiator's :class:`PathClient` instance or `None`.
        """
        return self._slots.get(AddressType.initiator)

    def set_initiator(self, initiator):
        """
        Set the initiator's :class:`PathClient` instance.

        Arguments:
            - `initiator`: A :class:`PathClient` instance.

        Return the previously set initiator or `None`.
        """
        previous_initiator = self._slots.get(AddressType.initiator)
        self._slots[AddressType.initiator] = initiator
        self.log.debug('Set initiator {}', initiator)
        # Update initiator's log name
        initiator.update_log_name(AddressType.initiator)
        # Authenticated, assign id
        initiator.authenticated = True
        initiator.id = AddressType.initiator
        # Return previous initiator
        return previous_initiator

    def get_responder(self, id_):
        """
        Return a responder's :class:`PathClient` instance or `None`.

        Arguments:
            - `id_`: The identifier of the responder.

        Raises :exc:`ValueError` if `id_` is not a valid responder
        identifier.
        """
        if not is_responder_id(id_):
            raise ValueError('Invalid responder identifier')
        return self._slots.get(id_)

    def get_responder_ids(self):
        """
        Return a list of responder's identifiers (slots).
        """
        return [id_ for id_, responder in self._slots.items()
                if is_responder_id(id_) and responder is not None]

    def add_responder(self, responder):
        """
        Set a responder's :class:`PathClient` instance.

        Arguments:
            - `client`: A :class:`PathClient` instance.

        Raises :exc:`SlotsFullError` if no free slot exists on the path.

        Return the assigned slot identifier.
        """
        for id_, client in self._slots.items():
            if is_responder_id(id_) and client is None:
                self._slots[id_] = responder
                self.log.debug('Added responder {}', responder)
                # Update responder's log name
                responder.update_log_name(id_)
                # Authenticated, set and return assigned slot id
                responder.authenticated = True
                responder.id = id_
                return id_
        raise SlotsFullError('No free slots on path')

    def remove_client(self, client):
        """
        Remove a client (initiator or responder) from the
        :class:`Path`.

        Arguments:
             - `client`: The :class:`PathClient` instance.

        Raises :exc:`ValueError` in case the client provided an
        invalid slot identifier.
        """

        if not client.authenticated:
            # Client has not been authenticated. Nothing to do.
            return
        id_ = client.id

        # Get client instance
        try:
            slot_client = self._slots[id_]
        except KeyError:
            raise ValueError('Invalid slot identifier: {}'.format(id_))

        # Compare client instances
        if client != slot_client:
            # Note: This is absolutely fine and happens when another initiator
            #        takes the place of a previous initiator.
            return

        # Remove client from slot
        self._slots[id_] = None
        self.log.debug('Removed {}', 'initiator' if is_initiator_id(id_) else 'responder')


class PathClient:
    __slots__ = (
        '_loop',
        '_connection',
        '_client_key',
        '_server_permanent_key',
        '_server_session_key',
        '_sequence_number_out',
        '_sequence_number_in',
        '_cookie_out',
        '_cookie_in',
        '_combined_sequence_number_out',
        '_combined_sequence_number_in',
        '_box',
        '_sign_box',
        '_id',
        '_keep_alive_interval',
        'log',
        'type',
        'authenticated',
        'keep_alive_timeout',
        'keep_alive_pings',
        '_task_queue'
    )

    def __init__(
            self, connection, path_number, initiator_key,
            server_session_key=None, loop=None
    ):
        self._loop = asyncio.get_event_loop() if loop is None else loop
        self._connection = connection
        self._client_key = initiator_key
        self._server_permanent_key = None
        self._server_session_key = server_session_key
        self._cookie_out = None
        self._cookie_in = None
        self._combined_sequence_number_out = None
        self._combined_sequence_number_in = None
        self._box = None
        self._sign_box = None
        self._id = AddressType.server
        self._keep_alive_interval = KEEP_ALIVE_INTERVAL_DEFAULT
        self.log = util.get_logger('path.{}.client.{:x}'.format(path_number, id(self)))
        self.type = None
        self.authenticated = False
        self.keep_alive_timeout = KEEP_ALIVE_TIMEOUT
        self.keep_alive_pings = 0

        # Queue for tasks to be run on the client (relay messages, closing, ...)
        self._task_queue = asyncio.Queue(loop=self._loop)

    def __str__(self):
        type_ = self.type
        if type_ is None:
            type_ = 'undetermined'
        return 'PathClient(role=0x{:02x}, id={}, at={})'.format(
            type_, self._id, hex(id(self)))

    @property
    def connection_closed(self):
        """
        Return the 'connection_closed' future of the underlying
        WebSocket connection.
        """
        return self._connection.connection_closed

    @property
    def id(self):
        """
        Return the assigned id on the :class:`Path`.
        """
        return self._id

    @id.setter
    def id(self, id_):
        """
        Assign the id. Only :class:`Path` may set the id!
        """
        self._id = id_
        self.log.debug('Assigned id: {}', id_)

    @property
    def keep_alive_interval(self):
        """
        Return the currently set keep alive interval.
        """
        return self._keep_alive_interval

    @keep_alive_interval.setter
    def keep_alive_interval(self, interval):
        """
        Assign a new keep alive interval. Will ignore values less than
        `KEEP_ALIVE_INTERVAL_MIN`.
        """
        if interval >= KEEP_ALIVE_INTERVAL_MIN:
            self._keep_alive_interval = interval

    @property
    def client_key(self):
        """
        Return the client's permanent key as :class:`bytes`.

        .. warning:: This is the initiator's key at default if no other
                     client key has been set!
        """
        return self._client_key

    @property
    def server_key(self):
        """
        Return the server's session :class:`libnacl.public.SecretKey`
        instance.
        """
        if self._server_session_key is None:
            self._server_session_key = libnacl.public.SecretKey()
        return self._server_session_key

    @property
    def server_permanent_key(self):
        """
        Return the server's permanent :class:`libnacl.public.SecretKey`
        instance chosen by the client.

        Raises `InternalError` in case the key has not been set, yet.
        """
        if self._server_permanent_key is None:
            raise InternalError("Server's permanent secret key instance not set")
        return self._server_permanent_key

    @server_permanent_key.setter
    def server_permanent_key(self, key):
        """
        Set the server's permanent :class:`libnacl.public.SecretKey`
        instance chosen by the client.
        """
        self._server_permanent_key = key

    @property
    def box(self):
        """
        Return the session's :class:`libnacl.public.Box` instance.
        """
        if self._box is None:
            self._box = libnacl.public.Box(self.server_key, self._client_key)
        return self._box

    @property
    def sign_box(self):
        """
        Return the :class:`libnacl.public.Box` instance that is used for
        signing the keys in the 'server-auth' message.

        Raises `InternalError` in case the server's permanent key has
        not been set, yet.
        """
        if self._sign_box is None:
            self._sign_box = libnacl.public.Box(
                self.server_permanent_key, self._client_key)
        return self._sign_box

    @property
    def cookie_out(self):
        """
        Return the cookie of the server (outgoing messages).
        """
        if self._cookie_out is None:
            self._cookie_out = os.urandom(COOKIE_LENGTH)
        return self._cookie_out

    @property
    def cookie_in(self):
        """
        Return the cookie of the client (incoming messages).
        """
        return self._cookie_in

    @property
    def combined_sequence_number_out(self):
        """
        Return the pending combined sequence number of the server
        (outgoing messages).
        """
        if self._combined_sequence_number_out is None:
            # Initialise the trailing 32 bits of the uint48 number with random bits
            initial_number, *_ = struct.unpack('!Q', b'\x00' * 4 + os.urandom(4))
            self._combined_sequence_number_out = initial_number
        return self._combined_sequence_number_out

    @combined_sequence_number_out.setter
    def combined_sequence_number_out(self, combined_sequence_number_out):
        """
        Update the combined sequence number of the server (outgoing
        messages). Will set the number to OverflowSentinel in case
        the number would overflow a 48-bit unsigned integer.
        """
        csn = self._validate_combined_sequence_number(combined_sequence_number_out)
        self._combined_sequence_number_out = csn

    @property
    def combined_sequence_number_in(self):
        """
        Return the pending combined sequence number of the client
        (incoming messages).
        """
        return self._combined_sequence_number_in

    @combined_sequence_number_in.setter
    def combined_sequence_number_in(self, combined_sequence_number_in):
        """
        Update the combined sequence number of the client (incoming
        messages). Will set the number to OverflowSentinel in case
        the number would overflow a 48-bit unsigned integer.
        """
        csn = self._validate_combined_sequence_number(combined_sequence_number_in)
        self._combined_sequence_number_in = csn

    @staticmethod
    def _validate_combined_sequence_number(combined_sequence_number):
        """
        Validate a combined sequence number.

        Return OverflowSentinel in case the number would overflow a
        48-bit unsigned integer, otherwise the passed sequence number.
        """
        if combined_sequence_number & 0xf000000000000 != 0:
            return OverflowSentinel
        else:
            return combined_sequence_number

    def set_client_key(self, public_key):
        """
        Set the public key of the client and update the internal box.

        Arguments:
            - `public_key`: A :class:`libnacl.public.PublicKey`.
        """
        self._client_key = public_key
        self._box = libnacl.public.Box(self.server_key, public_key)
        self.log.debug('Client key updated')

    def update_log_name(self, slot_id):
        """
        Update the logger's name by the assigned slot identifier.

        Arguments:
            - `slot_id`: The slot identifier of the client.
        """
        self.log.name += '.0x{:02x}'.format(slot_id)

    def valid_cookie(self, cookie_in):
        """
        Return `True` if the 16 byte cookie is the valid cookie of the
        client (or the cookie has not been set, yet).
        """
        if self.cookie_in is None:
            # Ensure that the client uses another cookie than we do
            if cookie_in == self.cookie_out:
                self.log.notice('Server and client cookies are the same')
                return False

            # First message: Set cookie
            self._cookie_in = cookie_in
            return True
        else:
            if cookie_in != self.cookie_in:
                self.log.notice('Client sent wrong cookie')
                return False
            return True

    def validate_combined_sequence_number(self, combined_sequence_number_in):
        """
        Set or validate the combined sequence number for incoming
        messages from the client.

        Raises:
            - :exc:`MessageError` in case this is the first message
              from the client and the leading 16 bits of the combined
              sequence number are not `0`.
            - :exc:`MessageError` in case the 6 byte combined sequence
              number is not the expected sequence number of the client.
            - :exc:`MessageFlowError` in case no messages can be
              received from the client as the combined sequence number
              has reset to `0`.
        """
        if self.combined_sequence_number_in is None:
            # Ensure that the leading 16 bits are 0
            if combined_sequence_number_in & 0xffff00000000 != 0:
                raise MessageError('Invalid sequence number, leading 16 bits are not 0')

            # First message: Set combined sequence number
            self._combined_sequence_number_in = combined_sequence_number_in

        # Ensure that the incoming CSN counter did not overflow
        if self.combined_sequence_number_in == OverflowSentinel:
            raise MessageFlowError(('Cannot receive any more messages, due to a '
                                    'sequence number counter overflow'))

        # Check that the CSN matches the expected CSN
        if combined_sequence_number_in != self.combined_sequence_number_in:
            raise MessageError('Invalid sequence number, expected {}, got {}'.format(
                self.combined_sequence_number_in, combined_sequence_number_in
            ))

    def p2p_allowed(self, destination_type):
        """
        Return `True` if :class:`RawMessage` instances are allowed and
        can be sent to the requested :class:`AddressType`.
        """
        return self.authenticated and self.type != destination_type

    @asyncio.coroutine
    def enqueue_task(self, coroutine_or_task):
        """
        Enqueue a coroutine or task into the task queue of the
        client.

        Arguments:
            - `coroutine_or_task`: A coroutine or a
              :class:`asyncio.Task`.
        """
        yield from self._task_queue.put(coroutine_or_task)

    @asyncio.coroutine
    def dequeue_task(self):
        """
        Dequeue and return a coroutine or task from the task queue of
        the client.

        Shall only be called from the client's :class:`Protocol`
        instance.
        """
        return (yield from self._task_queue.get())

    @asyncio.coroutine
    def send(self, message):
        """
        Disconnected
        MessageError
        MessageFlowError
        """
        # Pack
        self.log.debug('Packing message: {}', message.type)
        data = message.pack(self)
        self.log.trace('server >> {}', message)

        # Send data
        self.log.debug('Sending message')
        try:
            yield from self._connection.send(data)
        except websockets.ConnectionClosed as exc:
            self.log.debug('Connection closed while sending')
            raise Disconnected(exc.code) from exc

    @asyncio.coroutine
    def receive(self):
        """
        Disconnected
        """
        # Receive data
        try:
            data = yield from self._connection.recv()
        except websockets.ConnectionClosed as exc:
            self.log.debug('Connection closed while receiving')
            raise Disconnected(exc.code) from exc
        self.log.debug('Received message')

        # Unpack data and return
        message = unpack(self, data)
        self.log.debug('Unpacked message: {}', message.type)
        self.log.trace('server << {}', message)
        return message

    @asyncio.coroutine
    def ping(self):
        """
        Disconnected
        """
        self.log.debug('Sending ping')
        try:
            return (yield from self._connection.ping())
        except websockets.ConnectionClosed as exc:
            self.log.debug('Connection closed while pinging')
            raise Disconnected(exc.code) from exc

    @asyncio.coroutine
    def close(self, code=1000):
        # Note: We are not sending a reason for security reasons.
        yield from self._connection.close(code=code)


class Protocol:
    PATH_LENGTH = KEY_LENGTH * 2
