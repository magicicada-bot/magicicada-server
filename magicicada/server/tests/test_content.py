# Copyright 2008-2015 Canonical
# Copyright 2015-2018 Chicharreros (https://launchpad.net/~chicharreros)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# For further info, check  http://launchpad.net/magicicada-server

"""Test content operations."""

import logging
import os
import uuid
import zlib

from StringIO import StringIO

from magicicadaprotocol import (
    request,
    client as sp_client,
    errors as protoerrors,
    protocol_pb2,
)
from magicicadaprotocol.content_hash import (
    content_hash_factory,
    crc32,
    magic_hash_factory,
)
from mocker import Mocker, expect, ARGS, KWARGS, ANY
from twisted.internet import defer, reactor, threads, task, address
from twisted.trial.unittest import TestCase
from twisted.test.proto_helpers import StringTransport

from magicicada import settings
from magicicada.filesync import errors
from magicicada.filesync.models import StorageObject, StorageUser
from magicicada.server import server, diskstorage
from magicicada.server.content import (
    BaseUploadJob,
    BogusUploadJob,
    DBUploadJob,
    ContentManager,
    UploadJob,
    User,
    logger,
)
from magicicada.server.testing.testcase import (
    BufferedConsumer,
    FactoryHelper,
    TestWithDatabase,
)


NO_CONTENT_HASH = ""
EMPTY_HASH = content_hash_factory().content_hash()


def get_magic_hash(data):
    """Return the magic hash for the data."""
    magic_hash_object = magic_hash_factory()
    magic_hash_object.update(data)
    return magic_hash_object.content_hash()._magic_hash


class TestGetContent(TestWithDatabase):
    """Test get_content command."""

    def test_getcontent_unknown(self):
        """Get the content from an unknown file."""

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda r: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda r: client.get_content(
                    request.ROOT, r, request.UNKNOWN_HASH),
                client.test_fail)
            d.addCallbacks(client.test_fail, lambda x: client.test_done("ok"))

        return self.callback_test(auth)

    def test_getcontent_no_content(self):
        """Get the contents a file with no content"""
        file = self.usr0.root.make_file(u"file")

        def auth(client):
            """Do the real work """
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda r: client.get_root(), client.test_fail)
            d.addCallback(
                lambda r: client.get_content(request.ROOT, file.id, ''))
            d.addCallbacks(client.test_done, client.test_fail)
        d = self.callback_test(auth)
        self.assertFails(d, 'DOES_NOT_EXIST')
        return d

    def test_getcontent_not_owned_file(self):
        """Get the contents of a directory not owned by the user."""
        # create another user
        dir_id = self.usr1.root.make_subdirectory(u"subdir1").id

        # try to get the content of the directory with a different user
        def auth(client):
            """Do the real work """
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda r: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root_id: client.make_file(request.ROOT, root_id, "foo"),
                client.test_fail)
            d.addCallback(
                lambda r: client.get_content(request.ROOT, dir_id, EMPTY_HASH))
            d.addCallbacks(client.test_done, client.test_fail)

        d = self.callback_test(auth)
        self.assertFails(d, 'DOES_NOT_EXIST')
        return d

    def test_getcontent_empty_file(self):
        """Make sure get content of empty files work."""
        data = ""
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def check_file(req):
            if zlib.decompress(req.data) != "":
                raise Exception("data does not match")

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root_id: client.make_file(request.ROOT, root_id, "foo"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallback(lambda r: client.put_content(
                request.ROOT, r.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data)))
            d.addCallback(lambda _: client.get_content(
                          request.ROOT, self._state.req.new_id, EMPTY_HASH))
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)

        return self.callback_test(auth)

    def test_getcontent_file(self, check_file_content=True):
        """Get the content from a file."""
        data = "*" * 100000
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def check_file(req):
            if req.data != deflated_data:
                raise Exception("data does not match")

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, "hola"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(lambda _: client.get_content(
                          request.ROOT, self._state.req.new_id, hash_value))
            if check_file_content:
                d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def test_getcontent_cancel_after_other_request(self):
        """Simulate getting the cancel after another request in the middle."""
        data = os.urandom(100000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        # this is for the get content to send a lot of BYTES (which will leave
        # time for the cancel to arrive to the server) but not needing to
        # actually *put* a lot of content
        server.BytesMessageProducer.payload_size = 500

        # replace handle_GET_CONTENT so we can get the request reference
        def handle_get_content(s, message):
            """Handle GET_CONTENT message."""
            request = server.GetContentResponse(s, message)
            self.request = request
            request.start()
        self.patch(server.StorageServer, 'handle_GET_CONTENT',
                   handle_get_content)

        # monkeypatching to simulate that we're not working on that request
        # at the moment the CANCEL arrives
        orig_lie_method = server.GetContentResponse.processMessage

        def lie_about_current(self, *a, **k):
            """Lie that the request is not started."""
            self.started = False
            orig_lie_method(self, *a, **k)
            self.started = True
            server.GetContentResponse.processMessage = orig_lie_method

        server.GetContentResponse.processMessage = lie_about_current

        def auth(client):
            d = client.dummy_authenticate("open sesame")

            # monkeypatching to assure that the lock is released
            orig_check_method = server.GetContentResponse._processMessage

            def middle_check(innerself, *a, **k):
                """Check that the lock is released."""
                orig_check_method(innerself, *a, **k)
                self.assertFalse(innerself.protocol.request_locked)
                server.GetContentResponse._processMessage = orig_check_method
                d.addCallbacks(client.test_done, client.test_fail)

            server.GetContentResponse._processMessage = middle_check

            class HelperClass(object):

                def __init__(innerself):
                    innerself.cancelled = False
                    innerself.req = None

                def cancel(innerself, *args):
                    if innerself.cancelled:
                        return
                    innerself.cancelled = True

                    def _cancel(_):
                        """Directly cancel the server request."""
                        m = protocol_pb2.Message()
                        m.id = self.request.id
                        m.type = protocol_pb2.Message.CANCEL_REQUEST
                        self.request.cancel_message = m
                        self.request.processMessage(m)
                    d.addCallbacks(_cancel)

                def store_getcontent_result(innerself, req):
                    innerself.req = req

            hc = HelperClass()

            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, "hola"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT,
                    mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(lambda _: client.get_content_request(request.ROOT,
                          self._state.req.new_id, hash_value, 0, hc.cancel))
            d.addCallback(hc.store_getcontent_result)
        return self.callback_test(auth)

    def test_getcontent_cancel_inside_download(self):
        """Start to get the content from a file, and cancel in the middle."""
        data = os.urandom(100000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        # this is for the get content to send a lot of BYTES (which will leave
        # time for the cancel to arrive to the server) but not needing to
        # actually *put* a lot of content
        server.BytesMessageProducer.payload_size = 500

        # replace handle_GET_CONTENT so we can get the request reference
        def handle_get_content(s, message):
            """Handle GET_CONTENT message."""
            request = server.GetContentResponse(s, message)
            self.request = request
            request.start()
        self.patch(server.StorageServer, 'handle_GET_CONTENT',
                   handle_get_content)

        def auth(client):
            d = client.dummy_authenticate("open sesame")

            # monkeypatching to assure that the producer was cancelled
            orig_method = server.GetContentResponse.unregisterProducer

            def check(*a, **k):
                """Assure that it was effectively cancelled."""
                d.addCallbacks(client.test_done, client.test_fail)
                orig_method(*a, **k)
                server.GetContentResponse.unregisterProducer = orig_method

            server.GetContentResponse.unregisterProducer = check

            class HelperClass(object):

                def __init__(innerself):
                    innerself.cancelled = False
                    innerself.req = None

                def cancel(innerself, *args):
                    if innerself.cancelled:
                        return
                    innerself.cancelled = True

                    def _cancel(_):
                        """Directly cancel the server request."""
                        m = protocol_pb2.Message()
                        m.id = self.request.id
                        m.type = protocol_pb2.Message.CANCEL_REQUEST
                        self.request.cancel_message = m
                        self.request.processMessage(m)
                    d.addCallbacks(_cancel)

                def store_getcontent_result(innerself, req):
                    innerself.req = req

            hc = HelperClass()

            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, "hola"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT,
                    mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(lambda _: client.get_content_request(request.ROOT,
                          self._state.req.new_id, hash_value, 0, hc.cancel))
            d.addCallback(hc.store_getcontent_result)
        return self.callback_test(auth)

    def test_getcontent_cancel_after_download(self):
        """Start to get the content from a file, and cancel in the middle"""
        data = "*" * 100000
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):
            d = client.dummy_authenticate("open sesame")

            class HelperClass(object):

                def __init__(innerself):
                    innerself.cancelled = False
                    innerself.req = None
                    innerself.received = ""

                def cancel(innerself, newdata):
                    innerself.received += newdata
                    if innerself.received != deflated_data:
                        return

                    # got everything, now generate the cancel
                    if innerself.cancelled:
                        client.test_fail()
                    innerself.cancelled = True
                    d.addCallbacks(lambda _: innerself.req.cancel())
                    d.addCallbacks(client.test_done, client.test_fail)

                def store_getcontent_result(innerself, req):
                    innerself.req = req

            hc = HelperClass()

            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, "hola"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT,
                    mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(lambda _: client.get_content_request(request.ROOT,
                          self._state.req.new_id, hash_value, 0, hc.cancel))
            d.addCallback(hc.store_getcontent_result)
        return self.callback_test(auth)

    def test_getcontent_doesnt_exist(self):
        """Get the content from an unexistent node."""

        def auth(client):
            """Do the test."""
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(lambda _: client.get_content(request.ROOT,
                                                        uuid.uuid4(),
                                                        EMPTY_HASH),
                           client.test_fail)
            d.addCallbacks(client.test_done, client.test_fail)

        d = self.callback_test(auth)
        self.assertFails(d, 'DOES_NOT_EXIST')
        return d

    @defer.inlineCallbacks
    def test_when_to_release(self):
        """GetContent should assign resources before release."""
        storage_server = self.service.factory.buildProtocol('addr')
        mocker = Mocker()

        producer = mocker.mock()
        expect(producer.deferred).count(1).result(defer.Deferred())
        expect(producer.startProducing(ANY))

        node = mocker.mock()
        expect(node.deflated_size).result(0)
        expect(node.size).count(2).result(0)
        expect(node.content_hash).count(2).result('hash')
        expect(node.crc32).result(0)
        expect(node.get_content(KWARGS)).result(defer.succeed(producer))

        user = mocker.mock()
        expect(user.get_node(ARGS)
               ).result(defer.succeed(node))
        expect(user.username).result('')
        storage_server.user = user

        message = mocker.mock()
        expect(message.get_content.share).result(str(uuid.uuid4()))
        expect(message.get_content.node).count(3)
        expect(message.get_content.hash).count(2)
        expect(message.get_content.offset)

        self.patch(server.GetContentResponse, 'sendMessage', lambda *a: None)
        gc = server.GetContentResponse(storage_server, message)
        gc.id = 321

        # when GetContentResponse calls protocol.release(), it already
        # must have assigned the producer
        assigned = []
        storage_server.release = lambda a: assigned.append(gc.message_producer)
        with mocker:
            yield gc._start()
        self.assertNotEqual(assigned[0], None)


class TestPutContent(TestWithDatabase):
    """Test put_content command."""

    def setUp(self):
        """Set up."""
        d = super(TestPutContent, self).setUp()
        self.handler = self.add_memento_handler(server.logger, level=0)
        return d

    def test_putcontent_cancel(self):
        """Test putting content to a file and cancelling it."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def cancel_it(request):
                request.cancel()
                return request

            def test_done(request):
                if request.cancelled and request.finished:
                    client.test_done()
                else:
                    reactor.callLater(.1, test_done, request)

            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallback(
                lambda root: client.make_file(request.ROOT, root, "hola"))
            d.addCallback(
                lambda mkfile_req: client.put_content_request(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)))
            d.addCallback(cancel_it)

            def wait_and_trap(req):
                d = req.deferred
                d.addErrback(
                    lambda failure:
                    req if failure.check(request.RequestCancelledError)
                    else failure)
                return d

            d.addCallback(wait_and_trap)
            d.addCallbacks(test_done, client.test_fail)
            return d
        return self.callback_test(auth)

    def test_putcontent_cancel_after(self):
        """Test putting content to a file and cancelling it after finished."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallback(
                lambda root: client.make_file(request.ROOT, root, "hola"))
            d.addCallback(
                lambda mkfile_req: client.put_content_request(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)))
            d.addCallback(self.save_req, 'request')
            d.addCallback(lambda _: self._state.request.deferred)
            d.addCallback(lambda _: self._state.request.cancel())
            d.addCallbacks(client.test_done, client.test_fail)
            return d
        return self.callback_test(auth)

    def test_putcontent_cancel_middle(self):
        """Test putting content to a file and cancelling it in the middle."""
        size = int(settings.STORAGE_CHUNK_SIZE * 1.5)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)
        StorageUser.objects.filter(id=self.usr0.id).update(
            max_storage_bytes=size * 2)

        def auth(client):

            class Helper(object):

                def __init__(innerself):
                    innerself.notifs = 0
                    innerself.request = None
                    innerself.data = StringIO(deflated_data)

                def store_request(innerself, request):
                    innerself.request = request
                    return request

                def read(innerself, cant):
                    """If second read, cancel and trigger test."""
                    innerself.notifs += 1
                    if innerself.notifs == 2:
                        innerself.request.cancel()
                    if innerself.notifs > 2:
                        client.test_fail(ValueError("called beyond cancel!"))
                    return innerself.data.read(cant)
            helper = Helper()

            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallback(
                lambda root: client.make_file(request.ROOT, root, "hola"))
            d.addCallback(lambda mkfile_req: client.put_content_request(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, helper))
            d.addCallback(helper.store_request)
            d.addCallback(lambda request: request.deferred)
            d.addErrback(
                lambda failure:
                helper.request if failure.check(request.RequestCancelledError)
                else failure)
            d.addCallback(lambda _: client.test_done())
            return d
        return self.callback_test(auth)

    @defer.inlineCallbacks
    def test_putcontent(self, num_files=1, size=300000):
        """Test putting content to a file."""
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def auth(client):
            """Authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            # hook to test stats
            meter = []
            self.service.factory.metrics.meter = lambda *a: meter.append(a)
            gauge = []
            self.service.factory.metrics.gauge = lambda *a: gauge.append(a)

            for i in range(num_files):
                fname = 'hola_%d' % i
                mkfile_req = yield client.make_file(request.ROOT, root, fname)
                yield client.put_content(request.ROOT, mkfile_req.new_id,
                                         NO_CONTENT_HASH, hash_value,
                                         crc32_value, size, deflated_size,
                                         StringIO(deflated_data))

                try:
                    self.usr0.volume().get_content(hash_value)
                except errors.DoesNotExist:
                    raise ValueError("content blob is not there")

                # check upload stat and the offset sent
                self.assertTrue(('UploadJob.upload', 0) in gauge)
                self.assertTrue(('UploadJob.upload.begin', 1) in meter)
                self.handler.assert_debug(
                    "UploadJob begin content from offset 0")

        yield self.callback_test(auth, add_default_callbacks=True)

    def test_put_content_in_not_owned_file(self):
        """Test putting content in other user file"""
        # create another user
        file_id = self.usr1.root.make_file(u"a_dile").id
        # try to put the content in this file, but with other user
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):
            """do the real work"""
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda _: client.put_content(
                    request.ROOT, file_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallbacks(client.test_done, client.test_fail)
            return d

        d = self.callback_test(auth)
        self.assertFails(d, 'DOES_NOT_EXIST')
        return d

    @defer.inlineCallbacks
    def test_putcontent_duplicated(self):
        """Test putting the same content twice"""
        # check that only one object will be stored
        called = []
        ds = self.service.factory.diskstorage
        orig_put = ds.put
        ds.put = lambda *a: called.append(True) or orig_put(*a)
        yield self.test_putcontent(num_files=2)
        self.assertEqual(len(called), 1)

    def test_putcontent_twice_simple(self):
        """Test putting content twice."""
        data = "*" * 100
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def check_file(result):

                def _check_file():
                    try:
                        self.usr0.volume().get_content(hash_value)
                    except errors.DoesNotExist:
                        raise ValueError("content blob is not there")

                d = threads.deferToThread(_check_file)
                return d

            d = client.dummy_authenticate("open sesame")
            d.addCallback(lambda _: client.get_root())
            d.addCallback(lambda root_id:
                          client.make_file(request.ROOT, root_id, "hola"))
            d.addCallback(self.save_req, "file")
            d.addCallback(lambda req: client.put_content(
                request.ROOT, req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data)))
            d.addCallback(lambda _: client.put_content(request.ROOT,
                          self._state.file.new_id, hash_value, hash_value,
                          crc32_value, size, deflated_size,
                          StringIO(deflated_data)))
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def test_putcontent_twice_samefinal(self):
        """Test putting content twice."""
        data = "*" * 100
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def check_file(result):

                def _check_file():
                    try:
                        self.usr0.volume().get_content(hash_value)
                    except errors.DoesNotExist:
                        raise ValueError("content blob is not there")

                d = threads.deferToThread(_check_file)
                return d

            d = client.dummy_authenticate("open sesame")
            d.addCallback(lambda _: client.get_root())
            d.addCallback(lambda root_id:
                          client.make_file(request.ROOT, root_id, "hola"))
            d.addCallback(self.save_req, "file")
            d.addCallback(lambda req: client.put_content(
                request.ROOT, req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data)))

            # don't care about previous hash, as long the final hash is ok
            d.addCallback(lambda _: client.put_content(
                request.ROOT, self._state.file.new_id, NO_CONTENT_HASH,
                hash_value, crc32_value, size, deflated_size,
                StringIO(deflated_data)))
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def mkauth(self, data=None, previous_hash=None,
               hash_value=None, crc32_value=None, size=None):
        """Base function to create tests of wrong hints."""
        if data is None:
            data = "*" * 1000
        deflated_data = zlib.compress(data)
        if previous_hash is None:
            previous_hash = content_hash_factory().content_hash()
        if hash_value is None:
            hash_object = content_hash_factory()
            hash_object.update(data)
            hash_value = hash_object.content_hash()
        if crc32_value is None:
            crc32_value = crc32(data)
        if size is None:
            size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root_id: client.make_file(request.ROOT, root_id, "foo"),
                client.test_fail)
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT, mkfile_req.new_id, previous_hash, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallbacks(client.test_fail, lambda r: client.test_done("ok"))
        return auth

    def test_putcontent_bad_prev_hash(self):
        """Test wrong prev hash hint."""
        return self.callback_test(
            self.mkauth(previous_hash="sha1:notthehash"))

    def test_putcontent_bad_hash(self):
        """Test wrong hash hint."""
        return self.callback_test(
            self.mkauth(hash_value="sha1:notthehash"))

    def test_putcontent_bad_c3c32(self):
        """Test wrong crc32 hint."""
        return self.callback_test(
            self.mkauth(crc32_value=100))

    def test_putcontent_bad_size(self):
        """Test wrong size hint."""
        return self.callback_test(
            self.mkauth(size=20))

    def test_putcontent_notify(self):
        """Make sure put_content generates a notification."""
        data = "*" * 100000
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root_id: client.make_file(request.ROOT, root_id, "foo"),
                client.test_fail)
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT,
                    mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data)),
                client.test_fail)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def test_putcontent_nofile(self):
        """Test putting content to an inexistent file."""

        def auth(client):
            """Do the real work."""
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda r: client.get_root(), client.test_fail)
            args = (request.ROOT, uuid.uuid4(), '', '', 0, 0, 0, '')
            d.addCallbacks(lambda _: client.put_content(*args),
                           client.test_fail)
            d.addCallbacks(client.test_done, client.test_fail)

        d = self.callback_test(auth)
        self.assertFails(d, 'DOES_NOT_EXIST')
        return d

    def test_remove_uploadjob_deleted_file(self):
        """make sure we dont raise exceptions on deleted files"""
        so_file = self.usr0.root.make_file(u"foobar")
        upload_job = so_file.make_uploadjob(
            so_file.content_hash, "sha1:100", 0, 100)
        # kill file
        so_file.delete()
        upload_job.delete()

    def test_putcontent_conflict_middle(self):
        """Test putting content to a file and changing it in the middle."""
        data = os.urandom(3000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            class Helper(object):

                def __init__(innerself):
                    innerself.notifs = 0
                    innerself.request = None
                    innerself.data = StringIO(deflated_data)
                    innerself.node_id = None

                def save_node(innerself, node):
                    innerself.node_id = node.new_id
                    return node

                def store_request(innerself, request):
                    innerself.request = request
                    return request

                def read(innerself, data):
                    """Change the file when this client starts uploading it."""
                    # modify the file and cause a conflict
                    ho = content_hash_factory()
                    ho.update('randomdata')
                    hash_value = ho.content_hash()
                    filenode = self.usr0.get_node(innerself.node_id)
                    filenode.make_content(filenode.content_hash, hash_value,
                                          32, 1000, 1000, uuid.uuid4())
                    return innerself.data.read(data)

            helper = Helper()

            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallback(
                lambda root: client.make_file(request.ROOT, root, "hola"))
            d.addCallback(helper.save_node)
            d.addCallback(
                lambda mkfile_req: client.put_content_request(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size, helper))
            d.addCallback(helper.store_request)
            d.addCallback(lambda request: request.deferred)
            d.addErrback(
                lambda f:
                helper.request if f.check(protoerrors.ConflictError) else f)
            d.addCallback(lambda _: client.test_done())
            return d
        d = self.callback_test(auth)
        return d

    def test_putcontent_update_used_bytes(self):
        """Test putting content to a file and check that user used bytes
        is updated.
        """
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def check_used_bytes(result):

                def _check_file():
                    quota = StorageUser.objects.get(id=self.usr0.id)
                    self.assertEqual(size, quota.used_storage_bytes)

                d = threads.deferToThread(_check_file)
                return d

            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(lambda root: client.make_file(request.ROOT, root,
                                                         'hola_1'),
                           client.test_fail)
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(check_used_bytes)
            d.addCallbacks(client.test_done, client.test_fail)
            return d
        return self.callback_test(auth)

    @defer.inlineCallbacks
    def test_putcontent_quota_exceeded(self):
        """Test the QuotaExceeded handling."""
        StorageUser.objects.filter(id=self.usr0.id).update(max_storage_bytes=1)
        try:
            yield self.test_putcontent()
        except protoerrors.QuotaExceededError as e:
            self.assertEqual(e.free_bytes, 1)
            self.assertEqual(e.share_id, request.ROOT)
        else:
            self.fail('Should fail with QuotaExceededError!')

    def test_putcontent_generations(self):
        """Put content on a file and receive new generation."""
        data = os.urandom(30)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")

            # create the dir
            root_id = yield client.get_root()
            make_req = yield client.make_file(request.ROOT, root_id, "hola")

            # put content and check
            args = (request.ROOT, make_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data))
            putc_req = yield client.put_content(*args)
            self.assertEqual(putc_req.new_generation,
                             make_req.new_generation + 1)
        return self.callback_test(test, add_default_callbacks=True)

    def test_putcontent_corrupt(self):
        """Put content on a file with corrupt data."""
        data = os.urandom(30)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data) + 10
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")
            meter = []
            self.service.factory.metrics.meter = lambda *a: meter.append(a)

            # create the dir
            root_id = yield client.get_root()
            make_req = yield client.make_file(request.ROOT, root_id, "hola")

            # put content and check
            args = (request.ROOT, make_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data))
            try:
                yield client.put_content(*args)
            except Exception as ex:
                self.assertIsInstance(ex, protoerrors.UploadCorruptError)
            self.handler.assert_debug('UploadCorrupt', str(size))
        return self.callback_test(test, add_default_callbacks=True)

    @defer.inlineCallbacks
    def test_when_to_release(self):
        """PutContent should assign resources before release."""
        storage_server = self.service.factory.buildProtocol('addr')
        mocker = Mocker()

        user = mocker.mock()
        upload_job = mocker.mock()
        expect(user.get_upload_job(ARGS, KWARGS)).result(
            defer.succeed(upload_job))
        expect(upload_job.deferred).result(defer.succeed(None))
        expect(upload_job.offset).result(0)
        expect(upload_job.connect()).result(defer.succeed(None))
        expect(upload_job.upload_id).result("hola")
        expect(upload_job.storage_key).result("storage_key")
        expect(user.username).count(2).result('')
        storage_server.user = user

        message = mocker.mock()
        expect(message.put_content.share).result(str(uuid.uuid4()))
        expect(message.put_content.node).count(3)
        expect(message.put_content.previous_hash)
        expect(message.put_content.hash).count(2)
        expect(message.put_content.crc32)
        expect(message.put_content.size).count(2)
        expect(message.put_content.deflated_size)
        expect(message.put_content.magic_hash)
        expect(message.put_content.upload_id).count(2)

        self.patch(server.PutContentResponse, 'sendMessage',
                   lambda *r: None)
        pc = server.PutContentResponse(storage_server, message)
        pc.id = 123

        # when PutContentResponse calls protocol.release(), it already
        # must have assigned the upload job
        assigned = []
        storage_server.release = lambda r: assigned.append(pc.upload_job)

        with mocker:
            yield pc._start()
        self.assertEqual(assigned[0], upload_job)

    def test_putcontent_bad_data(self):
        """Test putting bad data to a file."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        # insert bad data in the deflated_data
        deflated_data = deflated_data[:10] + 'break it' + deflated_data[10:]

        @defer.inlineCallbacks
        def test(client):
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()
            mkfile_req = yield client.make_file(
                request.ROOT, root, u'a_file.txt')
            yield client.put_content(request.ROOT, mkfile_req.new_id,
                                     NO_CONTENT_HASH, hash_value, crc32_value,
                                     size, deflated_size,
                                     StringIO(deflated_data))
        d = self.callback_test(test, add_default_callbacks=True)
        self.assertFails(d, 'UPLOAD_CORRUPT')
        return d

    def _get_users(self, max_storage_bytes):
        """Get both storage and content users."""
        s_user = self.make_user(
            max_storage_bytes=max_storage_bytes)
        c_user = User(
            self.service.factory.content, s_user.id, s_user.root_volume_id,
            s_user.username, s_user.visible_name)
        return s_user, c_user

    @defer.inlineCallbacks
    def test_putcontent_handle_error_in_uploadjob_deferred(self):
        """PutContent should handle errors in upload_job.deferred.

        Test that a PutContent fails and is terminated as soon we get an
        error, instead of wait until the full upload is done.
        """
        chunk_size = settings.STORAGE_CHUNK_SIZE
        user, content_user = self._get_users(chunk_size ** 2)
        # create the file
        a_file = user.root.make_file(u"A new file")
        # build the upload data
        data = os.urandom(int(chunk_size * 1.5))
        size = len(data)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)

        # get a server instance
        storage_server = self.service.factory.buildProtocol('addr')
        storage_server.transport = StringTransport()
        # twisted 10.0.0 (lucid) returns an invalid peer in transport.getPeer()
        peerAddr = address.IPv4Address('TCP', '192.168.1.1', 54321)
        storage_server.transport.peerAddr = peerAddr
        storage_server.user = content_user
        storage_server.working_caps = server.PREFERRED_CAP

        message = protocol_pb2.Message()
        message.put_content.share = ''
        message.put_content.node = str(a_file.id)
        message.put_content.previous_hash = ''
        message.put_content.hash = hash_value
        message.put_content.crc32 = crc32_value
        message.put_content.size = size
        message.put_content.deflated_size = deflated_size
        message.id = 10
        message.type = protocol_pb2.Message.PUT_CONTENT

        begin_d = defer.Deferred()
        self.patch(server.PutContentResponse, 'sendMessage',
                   lambda *r: begin_d.callback(None))
        error_d = defer.Deferred()
        self.patch(
            server.PutContentResponse, 'sendError',
            lambda _, error, comment: error_d.callback((error, comment)))
        pc = server.PutContentResponse(storage_server, message)
        pc.id = 123

        # make the consumer crash
        def crash(*_):
            """Make it crash."""
            raise ValueError("test problem")
        self.patch(diskstorage.FileWriterConsumer, 'write', crash)

        # start uploading
        pc.start()
        # only one packet, in order to trigger the _start_receiving code
        # path.
        yield begin_d
        msg = protocol_pb2.Message()
        msg.type = protocol_pb2.Message.BYTES
        msg.bytes.bytes = deflated_data[:65536]
        pc._processMessage(msg)
        # check the error
        error_type, comment = yield error_d
        self.assertEqual(error_type, protocol_pb2.Error.TRY_AGAIN)
        self.assertEqual(comment, 'TryAgain (ValueError: test problem)')
        # check that the put_content response is properly termintated
        yield pc.deferred
        self.assertTrue(pc.finished)

    @defer.inlineCallbacks
    def _get_client_helper(self, auth_token):
        """Simplify the testing code by getting a client for the user."""
        connect_d = defer.Deferred()
        factory = FactoryHelper(connect_d.callback, caps=server.PREFERRED_CAP)
        connector = reactor.connectTCP("localhost", self.port, factory)
        client = yield connect_d
        yield client.dummy_authenticate(auth_token)
        defer.returnValue((client, connector))

    @defer.inlineCallbacks
    def test_putcontent_reuse_content_different_user_no_magic(self):
        """Different user with no magic hash: upload everything again."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        mhash_object = magic_hash_factory()
        mhash_object.update(data)

        client, connector = yield self._get_client_helper("open sesame")
        root = yield client.get_root()

        # first file, it should ask for all the content not magic here
        mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
        upload_req = self._put_content(client, mkfile_req.new_id,
                                       hash_value, crc32_value, size,
                                       deflated_data, None)
        yield upload_req.deferred

        # startup another client for a different user
        client.kill()
        connector.disconnect()
        client, connector = yield self._get_client_helper("usr3")
        root = yield client.get_root()
        mkfile_req = yield client.make_file(request.ROOT, root, 'chau')
        upload_req = self._put_content(client, mkfile_req.new_id,
                                       hash_value, crc32_value, size,
                                       deflated_data, None)
        yield upload_req.deferred

        # the BEGIN_CONTENT should be from 0
        message = [m for m in client.messages
                   if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
        self.assertEqual(message.begin_content.offset, 0)

        # check all went ok by getting the content
        get_req = yield client.get_content(request.ROOT,
                                           mkfile_req.new_id, hash_value)
        self.assertEqual(get_req.data, deflated_data)
        client.kill()
        connector.disconnect()

    @defer.inlineCallbacks
    def test_putcontent_reuse_content_different_user_with_magic(self):
        """Different user but with magic hash: don't upload all again."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        mhash_object = magic_hash_factory()
        mhash_object.update(data)
        mhash_value = mhash_object.content_hash()._magic_hash

        client, connector = yield self._get_client_helper("open sesame")
        root = yield client.get_root()

        # first file, it should ask for all the content not magic here
        mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
        upload_req = self._put_content(client, mkfile_req.new_id, hash_value,
                                       crc32_value, size, deflated_data, None)
        yield upload_req.deferred

        # hook to test stats
        meter = []
        self.service.factory.metrics.meter = lambda *a: meter.append(a)
        gauge = []
        self.service.factory.metrics.gauge = lambda *a: gauge.append(a)

        # startup another client for a different user.
        client.kill()
        connector.disconnect()
        client, connector = yield self._get_client_helper("usr3")
        root = yield client.get_root()

        mkfile_req = yield client.make_file(request.ROOT, root, 'chau')
        upload_req = self._put_content(client, mkfile_req.new_id, hash_value,
                                       crc32_value, size, deflated_data,
                                       mhash_value)
        resp = yield upload_req.deferred

        # the response should have the new_generation
        self.assertEqual(mkfile_req.new_generation + 1, resp.new_generation)

        # the BEGIN_CONTENT should be from the end
        message = [m for m in client.messages
                   if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
        self.assertEqual(message.begin_content.offset, deflated_size)

        # check all went ok by getting the content
        get_req = yield client.get_content(request.ROOT,
                                           mkfile_req.new_id, hash_value)
        self.assertEqual(get_req.data, deflated_data)
        # check reused content stat
        self.assertTrue(('MagicUploadJob.upload', deflated_size) in gauge)
        self.assertTrue(('MagicUploadJob.upload.begin', 1) in meter)
        client.kill()
        connector.disconnect()

    @defer.inlineCallbacks
    def test_putcontent_reuse_content_same_user_no_magic(self):
        """Same user doesn't upload everything even with no hash."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def auth(client):
            """Start authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            # first file, it should ask for all the content not magic here
            mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
            upload_req = self._put_content(client, mkfile_req.new_id,
                                           hash_value, crc32_value, size,
                                           deflated_data, None)
            yield upload_req.deferred

            # the BEGIN_CONTENT should be from 0
            message = [m for m in client.messages
                       if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
            self.assertEqual(message.begin_content.offset, 0)

            # hook to test stats
            meter = []
            self.service.factory.metrics.meter = lambda *a: meter.append(a)
            gauge = []
            self.service.factory.metrics.gauge = lambda *a: gauge.append(a)
            client.messages = []

            # other file but same content, still no magic
            mkfile_req = yield client.make_file(request.ROOT, root, 'chau')
            upload_req = self._put_content(client, mkfile_req.new_id,
                                           hash_value, crc32_value, size,
                                           deflated_data, None)
            resp = yield upload_req.deferred

            # response has the new generation in it
            self.assertEqual(
                resp.new_generation, mkfile_req.new_generation + 1)

            # the BEGIN_CONTENT should be from the end
            message = [m for m in client.messages
                       if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
            self.assertEqual(message.begin_content.offset, deflated_size)

            # check all went ok by getting the content
            get_req = yield client.get_content(request.ROOT,
                                               mkfile_req.new_id, hash_value)
            self.assertEqual(get_req.data, deflated_data)
            # check reused content stat
            self.assertTrue(('MagicUploadJob.upload', deflated_size) in gauge)
            self.assertTrue(('MagicUploadJob.upload.begin', 1) in meter)

        yield self.callback_test(auth, add_default_callbacks=True)

    def _put_content(self, client, new_id, hash_value, crc32_value,
                     size, deflated_data, magic_hash):
        """Put content to a file."""
        return client.put_content_request(
            request.ROOT, new_id, NO_CONTENT_HASH, hash_value, crc32_value,
            size, len(deflated_data), StringIO(deflated_data),
            magic_hash=magic_hash)

    @defer.inlineCallbacks
    def test_putcontent_reuse_content_same_user_with_magic(self):
        """Same user with magic hash: of course no new upload is needed."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        mhash_object = magic_hash_factory()
        mhash_object.update(data)
        mhash_value = mhash_object.content_hash()._magic_hash

        @defer.inlineCallbacks
        def auth(client):
            """Start authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            # first file, it should ask for all the content not magic here
            mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
            upload_req = self._put_content(client, mkfile_req.new_id,
                                           hash_value, crc32_value, size,
                                           deflated_data, mhash_value)
            yield upload_req.deferred

            # the BEGIN_CONTENT should be from 0
            message = [m for m in client.messages
                       if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
            self.assertEqual(message.begin_content.offset, 0)

            meter = []
            self.service.factory.metrics.meter = lambda *a: meter.append(a)
            gauge = []
            self.service.factory.metrics.gauge = lambda *a: gauge.append(a)
            client.messages = []

            # another file but same content, still no upload
            mkfile_req = yield client.make_file(request.ROOT, root, 'chau')
            upload_req = self._put_content(client, mkfile_req.new_id,
                                           hash_value, crc32_value, size,
                                           deflated_data, mhash_value)
            resp = yield upload_req.deferred

            # response has the new generation in it
            self.assertEqual(
                resp.new_generation, mkfile_req.new_generation + 1)

            # the BEGIN_CONTENT should be from the end
            message = [m for m in client.messages
                       if m.type == protocol_pb2.Message.BEGIN_CONTENT][0]
            self.assertEqual(message.begin_content.offset, deflated_size)

            # check all went ok by getting the content
            get_req = yield client.get_content(request.ROOT,
                                               mkfile_req.new_id, hash_value)
            self.assertEqual(get_req.data, deflated_data)
            # check reused content stat
            self.assertTrue(('MagicUploadJob.upload', deflated_size) in gauge)
            self.assertTrue(('MagicUploadJob.upload.begin', 1) in meter)

        yield self.callback_test(auth, add_default_callbacks=True)

    @defer.inlineCallbacks
    def test_putcontent_magic_hash(self):
        """Test that it calculated and stored the magic hash on put content."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        magic_hash_value = get_magic_hash(data)

        @defer.inlineCallbacks
        def auth(client):
            """Start authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
            yield client.put_content(request.ROOT, mkfile_req.new_id,
                                     NO_CONTENT_HASH, hash_value, crc32_value,
                                     size, deflated_size,
                                     StringIO(deflated_data))

            content_blob = self.usr0.volume().get_content(hash_value)
            self.assertEqual(content_blob.magic_hash, magic_hash_value)

        yield self.callback_test(auth, add_default_callbacks=True)

    def test_putcontent_blob_exists(self):
        """Test putting content with an existing blob (no magic)."""
        data = "*" * 100
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        # create the content blob without a magic hash in a different user.
        self.make_user(u'my_user', max_storage_bytes=2 ** 20)
        self.usr3.make_filepath_with_content(
            settings.ROOT_USERVOLUME_PATH + u"/file.txt", hash_value,
            crc32_value, size, deflated_size, uuid.uuid4())

        # overwrite UploadJob method to detect if it
        # uploaded stuff (it shouldn't)
        self.patch(BaseUploadJob, '_start_receiving',
                   lambda s: defer.fail(Exception("This shouldn't be called")))

        @defer.inlineCallbacks
        def auth(client):
            yield client.dummy_authenticate("open sesame")
            root_id = yield client.get_root()
            req = yield client.make_file(request.ROOT, root_id, "hola")
            yield client.put_content(request.ROOT, req.new_id, NO_CONTENT_HASH,
                                     hash_value, crc32_value, size,
                                     deflated_size, StringIO(deflated_data))

            # check it has content ok
            self.usr0.volume().get_content(hash_value)

        return self.callback_test(auth, add_default_callbacks=True)

    def test_put_content_on_a_dir_normal(self):
        """Test putting content in a dir."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        file_obj = StringIO(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")
            root_id = yield client.get_root()
            make_req = yield client.make_dir(request.ROOT, root_id, "hola")
            d = client.put_content(request.ROOT, make_req.new_id,
                                   NO_CONTENT_HASH, hash_value, crc32_value,
                                   size, deflated_size, file_obj)
            yield self.assertFailure(d, protoerrors.NoPermissionError)
        return self.callback_test(test, add_default_callbacks=True)

    def test_put_content_on_a_dir_magic(self):
        """Test putting content in a dir."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        file_obj = StringIO(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")
            root_id = yield client.get_root()

            # create a normal file
            make_req = yield client.make_file(request.ROOT, root_id, "hola")
            yield client.put_content(request.ROOT, make_req.new_id,
                                     NO_CONTENT_HASH, hash_value, crc32_value,
                                     size, deflated_size, file_obj)

            # create a dir and trigger a putcontent that will use 'magic'
            make_req = yield client.make_dir(request.ROOT, root_id, "chau")
            d = client.put_content(request.ROOT, make_req.new_id,
                                   NO_CONTENT_HASH, hash_value, crc32_value,
                                   size, deflated_size, file_obj)
            yield self.assertFailure(d, protoerrors.NoPermissionError)
        return self.callback_test(test, add_default_callbacks=True)


class TestMultipartPutContent(TestWithDatabase):
    """Test put_content using multipart command."""

    @defer.inlineCallbacks
    def setUp(self):
        """Set up."""
        self.handler = self.add_memento_handler(server.logger, level=0)
        yield super(TestMultipartPutContent, self).setUp()
        # override defaults set by TestWithDatabase.setUp.
        self.patch(settings, 'STORAGE_CHUNK_SIZE', 1024)

    def get_data(self, size):
        """Return random data of the specified size."""
        return os.urandom(size)

    @defer.inlineCallbacks
    def _test_putcontent(self, num_files=1, size=1024 * 1024):
        """Test putting content to a file."""
        data = self.get_data(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def auth(client):
            """Authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            # hook to test stats
            meter = []
            self.service.factory.metrics.meter = lambda *a: meter.append(a)
            gauge = []
            self.service.factory.metrics.gauge = lambda *a: gauge.append(a)

            for i in range(num_files):
                fname = 'hola_%d' % i
                mkfile_req = yield client.make_file(request.ROOT, root, fname)
                yield client.put_content(request.ROOT, mkfile_req.new_id,
                                         NO_CONTENT_HASH, hash_value,
                                         crc32_value, size, deflated_size,
                                         StringIO(deflated_data))

                try:
                    self.usr0.volume().get_content(hash_value)
                except errors.DoesNotExist:
                    raise ValueError("content blob is not there")
                # check upload stat and log, with the offset sent
                self.assertIn(('UploadJob.upload', 0), gauge)
                self.assertIn(('UploadJob.upload.begin', 1), meter)
                self.handler.assert_debug(
                    "UploadJob begin content from offset 0")

        yield self.callback_test(auth, timeout=self.timeout,
                                 add_default_callbacks=True)

    @defer.inlineCallbacks
    def test_resume_putcontent(self):
        """Test that the client can resume a putcontent request."""
        self.patch(settings, 'STORAGE_CHUNK_SIZE', 1024 * 64)
        size = 2 * 1024 * 512
        StorageUser.objects.filter(id=self.usr0.id).update(
            max_storage_bytes=size * 2)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        # setup
        connect_d = defer.Deferred()
        factory = FactoryHelper(connect_d.callback, caps=server.PREFERRED_CAP)
        connector = reactor.connectTCP("localhost", self.port, factory)
        client = yield connect_d
        yield client.dummy_authenticate("open sesame")

        # hook to test stats
        meter = []
        self.service.factory.metrics.meter = lambda *a: meter.append(a)
        gauge = []
        self.service.factory.metrics.gauge = lambda *a: gauge.append(a)

        # patch BytesMessageProducer in order to avoid sending the whole file
        orig_go = sp_client.BytesMessageProducer.go
        called = []

        def my_go(myself):
            data = myself.fh.read(request.MAX_PAYLOAD_SIZE)
            if len(called) >= 1:
                myself.request.error(EOFError("finish!"))
                myself.producing = False
                myself.finished = True
                return
            called.append(1)
            if data:
                response = protocol_pb2.Message()
                response.type = protocol_pb2.Message.BYTES
                response.bytes.bytes = data
                myself.request.sendMessage(response)
                reactor.callLater(0.1, myself.go)

        self.patch(sp_client.BytesMessageProducer, 'go', my_go)
        # we are authenticated
        root = yield client.get_root()
        filename = 'hola_12'
        mkfile_req = yield client.make_file(request.ROOT, root, filename)
        upload_info = []
        try:
            yield client.put_content(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                hash_value, crc32_value, size, deflated_size,
                StringIO(deflated_data),
                upload_id_cb=lambda *a: upload_info.append(a))
        except EOFError:
            # check upload stat and log, with the offset sent,
            # first time tarts from beginning.
            self.assertTrue(('UploadJob.upload', 0) in gauge)
            self.assertTrue(('UploadJob.upload.begin', 1) in meter)
            self.handler.assert_debug("UploadJob begin content from offset 0")
        else:
            self.fail("Should raise EOFError.")
        yield client.kill()
        yield connector.disconnect()

        # restore the BytesMessageProducer.go method
        self.patch(sp_client.BytesMessageProducer, 'go', orig_go)

        # connect a new client and try to upload again
        connect_d = defer.Deferred()
        factory = FactoryHelper(connect_d.callback, caps=server.PREFERRED_CAP)
        connector = reactor.connectTCP("localhost", self.port, factory)
        client = yield connect_d
        yield client.dummy_authenticate("open sesame")

        # restore patched client
        self.patch(sp_client.BytesMessageProducer, 'go', orig_go)

        processMessage = sp_client.PutContent.processMessage

        begin_content_d = defer.Deferred()

        def new_processMessage(myself, message):
            if message.type == protocol_pb2.Message.BEGIN_CONTENT:
                begin_content_d.callback(message)
            # call the original processMessage method
            return processMessage(myself, message)

        self.patch(sp_client.PutContent, 'processMessage', new_processMessage)
        req = sp_client.PutContent(
            client, request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
            hash_value, crc32_value, size, deflated_size,
            StringIO(deflated_data), upload_id=str(upload_info[0][0]))
        req.start()
        yield req.deferred

        message = yield begin_content_d
        offset_sent = message.begin_content.offset
        try:
            node_content = self.usr0.volume().get_content(hash_value)
        except errors.DoesNotExist:
            raise ValueError("content blob is not there")
        self.assertEqual(node_content.crc32, crc32_value)
        self.assertEqual(node_content.size, size)
        self.assertEqual(node_content.deflated_size, deflated_size)
        self.assertEqual(node_content.hash, hash_value)
        self.assertTrue(node_content.storage_key)

        # check upload stat and log, with the offset sent, second time it
        # resumes from the first chunk
        self.assertTrue(('UploadJob.upload', offset_sent) in gauge)
        self.handler.assert_debug(
            "UploadJob begin content from offset %d" % offset_sent)

        yield client.kill()
        yield connector.disconnect()

    @defer.inlineCallbacks
    def test_resume_putcontent_invalid_upload_id(self):
        """Client try to resume with an invalid upload_id.

        It receives a new upload_id.
        """
        self.patch(settings, 'STORAGE_CHUNK_SIZE', 1024 * 32)
        size = 2 * 1024 * 128
        StorageUser.objects.filter(id=self.usr0.id).update(
            max_storage_bytes=size * 2)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        # hook to test stats
        meter = []
        self.service.factory.metrics.meter = lambda *a: meter.append(a)
        gauge = []
        self.service.factory.metrics.gauge = lambda *a: gauge.append(a)

        @defer.inlineCallbacks
        def auth(client):
            """Make authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()
            mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
            upload_info = []
            req = client.put_content_request(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                hash_value, crc32_value, size, deflated_size,
                StringIO(deflated_data), upload_id="invalid id",
                upload_id_cb=lambda *a: upload_info.append(a))
            yield req.deferred
            self.assertTrue(('UploadJob.upload', 0) in gauge)
            self.assertTrue(('UploadJob.upload.begin', 1) in meter)
            self.handler.assert_debug(
                "UploadJob begin content from offset 0")
            self.assertEqual(len(upload_info), 1)
            upload_id, start_from = upload_info[0]
            self.assertIsInstance(uuid.UUID(upload_id), uuid.UUID)
            self.assertEqual(start_from, 0)

        yield self.callback_test(auth, add_default_callbacks=True)

    @defer.inlineCallbacks
    def test_putcontent_magic_hash(self):
        """Test that it calculated and stored the magic hash on put content."""
        data = os.urandom(30000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        magic_hash_value = get_magic_hash(data)

        @defer.inlineCallbacks
        def auth(client):
            """Make authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()
            mkfile_req = yield client.make_file(request.ROOT, root, 'hola')
            yield client.put_content(request.ROOT, mkfile_req.new_id,
                                     NO_CONTENT_HASH, hash_value, crc32_value,
                                     size, deflated_size,
                                     StringIO(deflated_data))

            content_blob = self.usr0.volume().get_content(hash_value)
            self.assertEqual(content_blob.magic_hash, magic_hash_value)
        yield self.callback_test(auth, add_default_callbacks=True)

    def test_putcontent_corrupt(self):
        """Put content on a file with corrupt data."""
        self.patch(settings, 'STORAGE_CHUNK_SIZE', 1024 * 64)
        size = 2 * 1024 * 512
        StorageUser.objects.filter(id=self.usr0.id).update(
            max_storage_bytes=size * 2)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data) + 10
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")

            # create the dir
            root_id = yield client.get_root()
            make_req = yield client.make_file(request.ROOT, root_id, "hola")

            # put content and check
            args = (request.ROOT, make_req.new_id, NO_CONTENT_HASH, hash_value,
                    crc32_value, size, deflated_size, StringIO(deflated_data))
            try:
                putc_req = client.put_content_request(*args)
                yield putc_req.deferred
            except Exception as ex:
                self.assertIsInstance(ex, protoerrors.UploadCorruptError)
            self.handler.assert_debug('UploadCorrupt', str(size))
            # check that the uploadjob was deleted.
            node = self.usr0.volume(None).get_node(make_req.new_id)
            self.assertRaises(errors.DoesNotExist,
                              node.get_multipart_uploadjob, putc_req.upload_id,
                              hash_value, crc32_value)

        return self.callback_test(test, add_default_callbacks=True)

    def test_putcontent_blob_exists(self):
        """Test putting content with an existing blob (no magic)."""
        data = self.get_data(1024 * 20)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        # create the content blob without a magic hash in a different user.
        self.make_user(u'my_user', max_storage_bytes=2 ** 20)
        self.usr3.make_filepath_with_content(
            settings.ROOT_USERVOLUME_PATH + u"/file.txt",
            hash_value, crc32_value, size, deflated_size, uuid.uuid4())

        def auth(client):

            def check_file(result):
                return threads.deferToThread(
                    lambda: self.usr0.volume().get_content(hash_value))

            d = client.dummy_authenticate("open sesame")
            d.addCallback(lambda _: client.get_root())
            d.addCallback(lambda root_id:
                          client.make_file(request.ROOT, root_id, "hola"))
            d.addCallback(self.save_req, "file")
            d.addCallback(lambda req: client.put_content(
                request.ROOT, req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data)))
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def test_put_content_on_a_dir(self):
        """Test putting content in a dir."""
        data = os.urandom(300000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)
        file_obj = StringIO(deflated_data)

        @defer.inlineCallbacks
        def test(client):
            """Test."""
            yield client.dummy_authenticate("open sesame")
            root_id = yield client.get_root()
            make_req = yield client.make_dir(request.ROOT, root_id, "hola")
            d = client.put_content(request.ROOT, make_req.new_id,
                                   NO_CONTENT_HASH, hash_value, crc32_value,
                                   size, deflated_size, file_obj)
            yield self.assertFailure(d, protoerrors.NoPermissionError)
        return self.callback_test(test, add_default_callbacks=True)


class TestMultipartPutContentGoodCompression(TestMultipartPutContent):
    """TestMultipartPutContent using data with a good compression ratio."""

    def get_data(self, size):
        """Return zero data of the specified size."""
        with open('/dev/zero', 'r') as source:
            return source.read(size) + os.urandom(size)


class TestPutContentInternalError(TestWithDatabase):
    """Test put_content command."""

    @defer.inlineCallbacks
    def test_putcontent_handle_internal_error_in_uploadjob_deferred(self):
        """PutContent should handle errors in upload_job.deferred.

        Test that a PutContent fails and is terminated as soon we get an
        error, instead of wait until the full upload is done.
        """
        chunk_size = settings.STORAGE_CHUNK_SIZE
        user = self.make_user(max_storage_bytes=chunk_size ** 2)
        content_user = User(
            self.service.factory.content, user.id, user.root_volume_id,
            user.username, user.visible_name)
        # create the file
        a_file = user.root.make_file(u"A new file")
        # build the upload data
        data = os.urandom(int(chunk_size * 1.5))
        size = len(data)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)

        # get a server instance
        storage_server = self.service.factory.buildProtocol('addr')
        storage_server.transport = StringTransport()
        # twisted 10.0.0 (lucid) returns an invalid peer in transport.getPeer()
        peerAddr = address.IPv4Address('TCP', '192.168.1.1', 54321)
        storage_server.transport.peerAddr = peerAddr
        storage_server.user = content_user
        storage_server.working_caps = server.PREFERRED_CAP

        message = protocol_pb2.Message()
        message.put_content.share = ''
        message.put_content.node = str(a_file.id)
        message.put_content.previous_hash = ''
        message.put_content.hash = hash_value
        message.put_content.crc32 = crc32_value
        message.put_content.size = size
        message.put_content.deflated_size = deflated_size
        message.id = 10
        message.type = protocol_pb2.Message.PUT_CONTENT

        begin_d = defer.Deferred()
        self.patch(server.PutContentResponse, 'sendMessage',
                   lambda *r: begin_d.callback(None))
        error_d = defer.Deferred()
        self.patch(
            server.PutContentResponse, 'sendError',
            lambda _, error, comment: error_d.callback((error, comment)))
        pc = server.PutContentResponse(storage_server, message)
        pc.id = 123

        # make the consumer crash
        def crash(*_):
            """Make it crash."""
            raise ValueError("Fail!")
        self.patch(BaseUploadJob, 'add_data', crash)

        # start uploading
        pc.start()
        # only one packet, in order to trigger the _start_receiving code
        # path.
        yield begin_d
        msg = protocol_pb2.Message()
        msg.type = protocol_pb2.Message.BYTES
        msg.bytes.bytes = deflated_data[:65536]
        pc._processMessage(msg)
        # check the error
        error_type, comment = yield error_d
        self.assertEqual(error_type, protocol_pb2.Error.INTERNAL_ERROR)
        self.assertEqual(comment, "Fail!")
        # check that the put_content response is properly termintated
        # and the server is shuttdown.
        yield storage_server.wait_for_shutdown()
        self.assertTrue(pc.finished)
        self.assertTrue(storage_server.shutting_down)

    @defer.inlineCallbacks
    def test_putcontent_handle_error_in_sendok(self):
        """PutContent should handle errors in send_ok."""
        data = os.urandom(1000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        @defer.inlineCallbacks
        def auth(client):
            """Authenticated test."""
            yield client.dummy_authenticate("open sesame")
            root = yield client.get_root()

            mkfile_req = yield client.make_file(request.ROOT, root, "hola")

            def breakit(*a):
                """Raise an exception to simulate the method call failed."""
                raise MemoryError("Simulated ME")

            self.patch(server.PutContentResponse, "_commit_uploadjob", breakit)

            d = client.put_content(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data))
            yield self.assertFailure(d, protoerrors.InternalError)

        yield self.callback_test(auth, add_default_callbacks=True)


class TestChunkedContent(TestWithDatabase):
    """ Tests operation on large data that requires multiple chunks """

    @defer.inlineCallbacks
    def setUp(self):
        """Setup the test."""
        yield super(TestChunkedContent, self).setUp()
        # tune the config for this tests
        self.patch(settings, 'STORAGE_CHUNK_SIZE', 1024 * 1024)

    def test_putcontent_chunked(self, put_fail=False, get_fail=False):
        """Checks a chunked putcontent."""
        size = int(settings.STORAGE_CHUNK_SIZE * 1.5)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def raise_quota(_):
                StorageUser.objects.filter(id=self.usr0.id).update(
                    max_storage_bytes=size * 2)

            def check_content(content):
                self.assertEqual(content.data, deflated_data)

            def _put_fail(result):
                # this will allow the server to split the data into chunks but
                # fail to put it back together in a single blob
                if put_fail:
                    # make the consumer crash
                    def crash(*_):
                        """Make it crash."""
                        raise ValueError("test problem")
                    self.patch(diskstorage.FileWriterConsumer, 'write', crash)
                return result

            def _get_fail(result):
                # this will allow the server to split the data into chunks but
                # fail to put it back together in a single blob
                if get_fail:
                    # make the producer crash
                    orig_func = diskstorage.FileReaderProducer.startProducing

                    def mitm(*a):
                        """MITM to return a failed deferred, not real one."""
                        deferred = orig_func(*a)
                        deferred.errback(ValueError())
                        return deferred
                    self.patch(diskstorage.FileReaderProducer,
                               'startProducing', mitm)
                return result

            d = client.dummy_authenticate("open sesame")
            d.addCallback(raise_quota)
            d.addCallback(lambda _: client.get_root())
            d.addCallback(lambda root_id: client.make_file(request.ROOT,
                                                           root_id, "hola"))
            d.addCallback(self.save_req, 'req')
            d.addCallback(_put_fail)
            d.addCallback(_get_fail)
            d.addCallback(lambda mkfile_req: client.put_content(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data)))
            d.addCallback(lambda _: client.get_content(request.ROOT,
                                                       self._state.req.new_id,
                                                       hash_value))
            if not put_fail and not get_fail:
                d.addCallback(check_content)
            d.addCallbacks(client.test_done, client.test_fail)
            return d
        return self.callback_test(auth, timeout=10)

    def test_putcontent_chunked_putfail(self):
        """Assures that chunked putcontent fails with "try again"."""
        d = self.test_putcontent_chunked(put_fail=True)
        self.assertFails(d, 'TRY_AGAIN')
        return d

    def test_putcontent_chunked_getfail(self):
        """Assures that chunked putcontent fails with "try again"."""
        d = self.test_putcontent_chunked(get_fail=True)
        self.assertFails(d, 'NOT_AVAILABLE')
        return d

    def test_deferred_add_part_to_uj(self):
        """Check that parts are added to upload job only after a limit."""
        size = int(settings.STORAGE_CHUNK_SIZE * 2.5)
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)

        recorded_calls = []
        orig_call = self.service.rpc_dal.call

        def recording_call(method, **parameters):
            if method == 'add_part_to_uploadjob':
                recorded_calls.append(parameters)
            return orig_call(method, **parameters)

        self.service.rpc_dal.call = recording_call

        @defer.inlineCallbacks
        def test(client):
            yield client.dummy_authenticate("open sesame")
            StorageUser.objects.filter(id=self.usr0.id).update(
                max_storage_bytes=size * 2)
            root = yield client.get_root()
            mkfile_req = yield client.make_file(request.ROOT, root, "hola")
            putcontent_req = yield client.put_content_request(
                request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH, hash_value,
                crc32_value, size, deflated_size, StringIO(deflated_data))
            yield putcontent_req.deferred

            # check calls; there should be only 2, as size == chunk size * 2.5
            self.assertEqual(len(recorded_calls), 2)

        return self.callback_test(test, add_default_callbacks=True)


class UserTest(TestWithDatabase):
    """Test User functionality."""

    @defer.inlineCallbacks
    def setUp(self):
        yield super(UserTest, self).setUp()

        # user and root to use in the tests
        u = self.suser = self.make_user(max_storage_bytes=64 ** 2)
        self.user = User(
            self.service.factory.content, u.id, u.root_volume_id, u.username,
            u.visible_name)

    @defer.inlineCallbacks
    def test_make_file_node_with_gen(self):
        """Test that make_file returns a node with generation in it."""
        root_id, root_gen = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        _, generation, _ = yield self.user.make_file(volume_id, root_id,
                                                     u"name", True)
        self.assertEqual(generation, root_gen + 1)

    @defer.inlineCallbacks
    def test_make_dir_node_with_gen(self):
        """Test that make_dir returns a node with generation in it."""
        root_id, root_gen = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        _, generation, _ = yield self.user.make_dir(volume_id, root_id,
                                                    u"name", True)
        self.assertEqual(generation, root_gen + 1)

    @defer.inlineCallbacks
    def test_unlink_node_with_gen(self):
        """Test that unlink returns a node with generation in it."""
        root_id, root_gen = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        node_id, generation, _ = yield self.user.make_dir(volume_id, root_id,
                                                          u"name", True)
        new_gen, kind, name, _ = yield self.user.unlink_node(
            volume_id, node_id)
        self.assertEqual(new_gen, generation + 1)
        self.assertEqual(kind, StorageObject.DIRECTORY)
        self.assertEqual(name, u"name")

    @defer.inlineCallbacks
    def test_move_node_with_gen(self):
        """Test that move returns a node with generation in it."""
        root_id, _ = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        yield self.user.make_dir(volume_id, root_id, u"name", True)
        node_id, generation, _ = yield self.user.make_dir(volume_id, root_id,
                                                          u"name", True)
        new_generation, _ = yield self.user.move(volume_id, node_id,
                                                 root_id, u"new_name")
        self.assertEqual(new_generation, generation + 1)

    @defer.inlineCallbacks
    def test_get_upload_job(self):
        """Test for _get_upload_job."""
        root_id, _ = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        node_id, _, _ = yield self.user.make_file(volume_id, root_id,
                                                  u"name", True)
        size = 1024
        # this will create a new uploadjob
        upload_job = yield self.user.get_upload_job(
            None, node_id, NO_CONTENT_HASH, 'foo', 10, size / 2, size / 4,
            True)
        self.assertIsInstance(upload_job, UploadJob)

    @defer.inlineCallbacks
    def test_get_free_bytes_root(self):
        """Get the user free bytes, normal case."""
        StorageUser.objects.filter(id=self.suser.id).update(
            max_storage_bytes=1000)
        fb = yield self.user.get_free_bytes()
        self.assertEqual(fb, 1000)

    @defer.inlineCallbacks
    def test_get_free_bytes_own_share(self):
        """Get the user free bytes asking for same user's share."""
        other_user = self.make_user(username=u'user2')
        share = self.suser.root.share(other_user.id, u"sharename")
        StorageUser.objects.filter(id=self.suser.id).update(
            max_storage_bytes=1000)
        fb = yield self.user.get_free_bytes(share.id)
        self.assertEqual(fb, 1000)

    @defer.inlineCallbacks
    def test_get_free_bytes_othershare_ok(self):
        """Get the user free bytes for other user's share."""
        other_user = self.make_user(username=u'user2', max_storage_bytes=500)
        share = other_user.root.share(self.suser.id, u"sharename")
        fb = yield self.user.get_free_bytes(share.id)
        self.assertEqual(fb, 500)

    @defer.inlineCallbacks
    def test_get_free_bytes_othershare_bad(self):
        """Get the user free bytes for a share of a user that is not valid."""
        other_user = self.make_user(username=u'user2', max_storage_bytes=500)
        share = other_user.root.share(self.suser.id, u"sharename")
        StorageUser.objects.filter(id=other_user.id).update(is_active=False)
        d = self.user.get_free_bytes(share.id)
        yield self.assertFailure(d, errors.DoesNotExist)

    @defer.inlineCallbacks
    def test_change_public_access(self):
        """Test change public access action."""
        root_id, root_gen = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)
        node_id, generation, _ = yield self.user.make_file(
            volume_id, root_id, u"name")
        public_url = yield self.user.change_public_access(
            volume_id, node_id, True)
        self.assertTrue(public_url.startswith(settings.PUBLIC_URL_PREFIX))

    @defer.inlineCallbacks
    def test_list_public_files(self):
        """Test the public files listing."""
        root_id, _ = yield self.user.get_root()
        volume_id = yield self.user.get_volume_id(root_id)

        # create three files, make two public
        node_id_1, _, _ = yield self.user.make_file(
            volume_id, root_id, u"name1")
        yield self.user.make_file(volume_id, root_id, u"name2")
        node_id_3, _, _ = yield self.user.make_file(
            volume_id, root_id, u"name3")
        yield self.user.change_public_access(volume_id, node_id_1, True)
        yield self.user.change_public_access(volume_id, node_id_3, True)

        public_files = yield self.user.list_public_files()
        self.assertEqual(set(node.id for node in public_files),
                         {node_id_1, node_id_3})


class TestUploadJob(TestWithDatabase):
    """Tests for UploadJob class."""

    upload_class = UploadJob

    @defer.inlineCallbacks
    def setUp(self):
        """Setup the test."""
        yield super(TestUploadJob, self).setUp()
        self.chunk_size = settings.STORAGE_CHUNK_SIZE
        self.half_size = self.chunk_size / 2
        self.double_size = self.chunk_size * 2
        self.user = self.make_user(max_storage_bytes=self.chunk_size ** 2)
        self.content_user = User(
            self.service.factory.content, self.user.id,
            self.user.root_volume_id, self.user.username,
            self.user.visible_name)

        def slowScheduler(x):
            """A slower scheduler for our cooperator."""
            return reactor.callLater(0.1, x)

        self._cooperator = task.Cooperator(scheduler=slowScheduler)
        self.addCleanup(self._cooperator.stop)

    @defer.inlineCallbacks
    def make_upload(self, size):
        """Create the storage UploadJob object.

        @param size: the size of the upload
        @return: a tuple (deflated_data, hash_value, upload_job)
        """
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        magic_hash_value = get_magic_hash(data)
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)
        root, _ = yield self.content_user.get_root()
        c_user = self.content_user
        r = yield self.service.factory.content.rpc_dal.call(
            'make_file_with_content',
            user_id=c_user.id, volume_id=self.user.root_volume_id,
            parent_id=root, name=u"A new file",
            node_hash=EMPTY_HASH, crc32=0,
            size=0, deflated_size=0, storage_key=None)
        node_id = r['node_id']
        node = yield c_user.get_node(self.user.root_volume_id, node_id, None)
        args = (c_user, self.user.root_volume_id, node_id, node.content_hash,
                hash_value, crc32_value, size)
        upload = yield DBUploadJob.make(*args)
        upload_job = self.upload_class(c_user, node, node.content_hash,
                                       hash_value, crc32_value, size,
                                       deflated_size, None, False,
                                       magic_hash_value, upload)
        defer.returnValue((deflated_data, hash_value, upload_job))

    @defer.inlineCallbacks
    def test_simple_upload(self):
        """Test UploadJob without scatter/gather."""
        size = self.half_size
        deflated_data, hash_value, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        yield upload_job.commit()
        node_id = upload_job.file_node.id
        node = yield self.content_user.get_node(self.user.root_volume_id,
                                                node_id, None)
        self.assertEqual(node.content_hash, hash_value)

    @defer.inlineCallbacks
    def test_chunked_upload(self):
        """Test UploadJob with chunks."""
        size = self.double_size
        deflated_data, hash_value, upload_job = yield self.make_upload(size)
        yield upload_job.connect()

        # now let's upload some data
        def data_iter(chunk_size=request.MAX_MESSAGE_SIZE):
            """Iterate over chunks."""
            for part in range(0, len(deflated_data), chunk_size):
                yield upload_job.add_data(
                    deflated_data[part:part + chunk_size])

        yield self._cooperator.coiterate(data_iter())
        yield upload_job.commit()

        # verify node content
        node_id = upload_job.file_node.id
        node = yield self.content_user.get_node(self.user.root_volume_id,
                                                node_id, None)
        self.assertEqual(node.content_hash, hash_value)

    @defer.inlineCallbacks
    def test_upload_fail_with_conflict(self):
        """Test UploadJob conflict."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        # poison the upload
        upload_job.original_file_hash = "sha1:fakehash"
        try:
            yield upload_job.commit()
        except server.errors.ConflictError as e:
            self.assertEqual(str(e), 'The File changed while uploading.')
        else:
            self.fail("Should fail with ConflictError")

    @defer.inlineCallbacks
    def test_upload_corrupted_deflated(self):
        """Test corruption of deflated data in UploadJob."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        # change the deflated data to trigger a UploadCorrupt error
        yield upload_job.add_data(deflated_data + '10')
        try:
            yield upload_job.commit()
        except server.errors.UploadCorrupt as e:
            self.assertEqual(str(e), upload_job._deflated_size_hint_mismatch)
        else:
            self.fail("Should fail with UploadCorrupt")

    @defer.inlineCallbacks
    def test_upload_corrupted_inflated(self):
        """Test corruption of inflated data in UploadJob."""
        # now test corruption of the inflated data
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        # change the inflated size hint to trigger the error
        upload_job.producer.inflated_size += 10
        try:
            yield upload_job.commit()
        except server.errors.UploadCorrupt as e:
            self.assertEqual(str(e), upload_job._inflated_size_hint_mismatch)
        else:
            self.fail("Should fail with UploadCorrupt")

    @defer.inlineCallbacks
    def test_upload_corrupted_hash(self):
        """Test corruption of hash in UploadJob."""
        # now test corruption of the content hash hint
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        upload_job.hash_hint = 'sha1:fakehash'
        yield upload_job.add_data(deflated_data)
        try:
            yield upload_job.commit()
        except server.errors.UploadCorrupt as e:
            self.assertEqual(str(e), upload_job._content_hash_hint_mismatch)
        else:
            self.fail("Should fail with UploadCorrupt")

    @defer.inlineCallbacks
    def test_upload_corrupted_magic_hash(self):
        """Test corruption of magic hash in UploadJob."""
        # now test corruption of the content hash hint
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        upload_job.magic_hash = 'sha1:fakehash'
        yield upload_job.add_data(deflated_data)
        try:
            yield upload_job.commit()
        except server.errors.UploadCorrupt as e:
            self.assertEqual(str(e), upload_job._magic_hash_hint_mismatch)
        else:
            self.fail("Should fail with UploadCorrupt")

    @defer.inlineCallbacks
    def test_upload_corrupted_crc32(self):
        """Test corruption of crc32 in UploadJob."""
        # now test corruption of the crc32 hint
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        upload_job.crc32_hint = 'bad crc32'
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        try:
            yield upload_job.commit()
        except server.errors.UploadCorrupt as e:
            self.assertEqual(str(e), upload_job._crc32_hint_mismatch)
        else:
            self.fail("Should fail with UploadCorrupt")

    @defer.inlineCallbacks
    def test_commit_return_node_with_gen(self):
        """Commit return the node with the updated generation."""
        size = self.half_size
        deflated_data, hash_value, upload_job = yield self.make_upload(size)
        previous_generation = upload_job.file_node.generation
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        new_generation = yield upload_job.commit()
        self.assertEqual(new_generation, previous_generation + 1)

    @defer.inlineCallbacks
    def test_add_bad_data(self):
        """Test UploadJob.add_data with invalid data."""
        size = self.half_size
        deflated_data, hash_value, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data('Neque porro quisquam est qui dolorem ipsum')
        self.assertFailure(upload_job.deferred, server.errors.UploadCorrupt)
        yield upload_job.cancel()

    @defer.inlineCallbacks
    def test_upload_id(self):
        """Test the upload_id generation."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        self.assertEqual(upload_job.upload_id,
                         upload_job.uploadjob.multipart_key)

    @defer.inlineCallbacks
    def test_stop_sets_canceling(self):
        """Set canceling on stop."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        assert not upload_job.canceling
        upload_job.stop()
        self.assertTrue(upload_job.canceling)

    @defer.inlineCallbacks
    def test_unregisterProducer_on_cancel(self):
        """unregisterProducer is never called"""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        mocker = Mocker()
        producer = mocker.mock()
        self.patch(upload_job, 'producer', producer)
        expect(producer.stopProducing())
        with mocker:
            yield upload_job.cancel()

    @defer.inlineCallbacks
    def test_unregisterProducer_on_stop(self):
        """unregisterProducer isn't called."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        mocker = Mocker()
        producer = mocker.mock()
        expect(producer.stopProducing())
        self.patch(upload_job, 'producer', producer)
        with mocker:
            yield upload_job.stop()

    @defer.inlineCallbacks
    def test_commit_and_delete_fails(self):
        """Commit and delete fails, log in warning."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        # make commit fail
        self.patch(upload_job, "_commit",
                   lambda: defer.fail(ValueError("boom")))
        # also delete
        self.patch(upload_job.uploadjob, "delete",
                   lambda: defer.fail(ValueError("delete boom")))
        handler = self.add_memento_handler(logger, level=logging.WARNING)
        failure = yield self.assertFailure(upload_job.commit(), ValueError)
        self.assertEqual(str(failure), "delete boom")
        handler.assert_exception("delete boom")

    @defer.inlineCallbacks
    def test_delete_after_commit_ok(self):
        """Delete the UploadJob after succesful commit."""
        size = self.half_size
        deflated_data, _, upload_job = yield self.make_upload(size)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        yield upload_job.commit()
        node = upload_job.file_node
        # check that the upload is no more
        d = DBUploadJob.get(
            self.content_user, node.volume_id, node.id, upload_job.upload_id,
            upload_job.hash_hint, upload_job.crc32_hint)
        yield self.assertFailure(d, errors.DoesNotExist)

    @defer.inlineCallbacks
    def test_add_operation_ok(self):
        _, _, upload_job = yield self.make_upload(20)
        called = []

        def fake_operation(_):
            called.append('operation')

        def fake_error_handler(_):
            called.append('error')

        upload_job.add_operation(fake_operation, fake_error_handler)
        yield upload_job.ops
        self.assertEqual(called, ['operation'])

    @defer.inlineCallbacks
    def test_add_operation_error(self):
        _, _, upload_job = yield self.make_upload(20)
        called = []

        def crash(_):
            called.append('operation')
            raise ValueError("crash")

        def fake_error_handler(failure):
            called.append('error: ' + str(failure.value))

        upload_job.add_operation(crash, fake_error_handler)
        yield upload_job.ops
        self.assertEqual(called, ['operation', 'error: crash'])

    @defer.inlineCallbacks
    def test_add_data_after_cancel(self):
        """Data after cancellation should be just ignored."""
        deflated_data, _, upload_job = yield self.make_upload(self.half_size)
        middle = self.half_size // 2
        data1, data2 = deflated_data[:middle], deflated_data[middle:]
        yield upload_job.connect()
        yield upload_job.add_data(data1)
        yield upload_job.cancel()
        yield upload_job.add_data(data2)


class TestNode(TestWithDatabase):
    """Tests for Node class."""

    upload_class = UploadJob

    @defer.inlineCallbacks
    def setUp(self):
        """Setup the test."""
        yield super(TestNode, self).setUp()
        self.chunk_size = settings.STORAGE_CHUNK_SIZE
        self.half_size = self.chunk_size / 2
        self.double_size = self.chunk_size * 2
        self.user = self.make_user(max_storage_bytes=self.chunk_size ** 2)
        self.suser = User(
            self.service.factory.content, self.user.id,
            self.user.root_volume_id, self.user.username,
            self.user.visible_name)

        # add a memento handler, to check we log ok
        self.handler = self.add_memento_handler(server.logger)

    @defer.inlineCallbacks
    def _upload_a_file(self, user, content_user):
        """Upload a file.

        @param user: the storage user
        @param content: the User
        @return: a tuple (upload, deflated_data)
        """
        size = self.chunk_size / 2
        data = os.urandom(size)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        magic_hash_value = get_magic_hash(data)
        crc32_value = crc32(data)
        deflated_size = len(deflated_data)
        root, _ = yield content_user.get_root()
        r = yield self.service.factory.content.rpc_dal.call(
            'make_file_with_content', user_id=content_user.id,
            volume_id=self.user.root_volume_id, parent_id=root,
            name=u"A new file", node_hash=EMPTY_HASH, crc32=0,
            size=0, deflated_size=0, storage_key=None)
        node_id = r['node_id']
        node = yield content_user.get_node(user.root_volume_id, node_id, None)
        args = (content_user, self.user.root_volume_id, node_id,
                node.content_hash, hash_value, crc32_value, size)
        upload = yield DBUploadJob.make(*args)
        upload_job = UploadJob(
            content_user, node, node.content_hash, hash_value, crc32_value,
            size, deflated_size, None, False, magic_hash_value, upload)
        yield upload_job.connect()
        yield upload_job.add_data(deflated_data)
        yield upload_job.commit()
        node = yield content_user.get_node(user.root_volume_id, node_id, None)
        self.assertEqual(hash_value, node.content_hash)
        defer.returnValue((node, deflated_data))

    @defer.inlineCallbacks
    def test_get_content(self):
        """Test for Node.get_content 'all good' code path."""
        node, deflated_data = yield self._upload_a_file(self.user, self.suser)
        producer = yield node.get_content(previous_hash=node.content_hash)
        consumer = BufferedConsumer(producer)
        # resume producing
        producer.startProducing(consumer)
        yield producer.deferred
        self.assertEqual(len(consumer.buffer.getvalue()), len(deflated_data))
        self.assertEqual(consumer.buffer.getvalue(), deflated_data)

    @defer.inlineCallbacks
    def _get_user_node(self):
        """Get a user and a node."""
        node, deflated_data = yield self._upload_a_file(self.user, self.suser)
        defer.returnValue((self.suser, node))

    @defer.inlineCallbacks
    def test_handles_producing_error(self):
        user, node = yield self._get_user_node()

        # make the consumer crash
        orig_func = diskstorage.FileReaderProducer.startProducing

        def mitm(*a):
            """MITM to return a failed deferred instead of real one."""
            deferred = orig_func(*a)
            deferred.errback(ValueError("crash"))
            return deferred
        self.patch(diskstorage.FileReaderProducer, 'startProducing', mitm)

        producer = yield node.get_content(previous_hash=node.content_hash,
                                          user=user)
        consumer = BufferedConsumer(producer)
        # resume producing
        producer.startProducing(consumer)
        yield self.assertFailure(producer.deferred, server.errors.NotAvailable)


class TestGenerations(TestWithDatabase):
    """Tests for generations related methods."""

    @defer.inlineCallbacks
    def setUp(self):
        """Setup the test."""
        yield super(TestGenerations, self).setUp()
        self.suser = u = self.make_user(max_storage_bytes=64 ** 2)
        self.user = User(
            self.service.factory.content, u.id, u.root_volume_id, u.username,
            u.visible_name)

    @defer.inlineCallbacks
    def test_get_delta_empty(self):
        """Test that User.get_delta works as expected."""
        delta = yield self.user.get_delta(None, 0)
        free_bytes = self.suser.free_bytes
        self.assertEqual(delta, ([], 0, free_bytes))

    @defer.inlineCallbacks
    def test_get_delta_from_0(self):
        """Test that User.get_delta works as expected."""
        nodes = [self.suser.root.make_file(u"name%s" % i) for i in range(5)]
        delta, end_gen, free_bytes = yield self.user.get_delta(None, 0)
        self.assertEqual(len(delta), len(nodes))
        self.assertEqual(end_gen, nodes[-1].generation)
        self.assertEqual(free_bytes, self.suser.free_bytes)

    @defer.inlineCallbacks
    def test_get_delta_from_middle(self):
        """Test that User.get_delta works as expected."""
        # create some nodes
        root = self.suser.root
        nodes = [root.make_file(u"name%s" % i) for i in range(5)]
        nodes += [root.make_subdirectory(u"dir%s" % i) for i in range(5)]
        from_generation = nodes[5].generation
        delta, end_gen, free_bytes = yield self.user.get_delta(None,
                                                               from_generation)
        self.assertEqual(len(delta), len(nodes[6:]))
        self.assertEqual(end_gen, nodes[-1].generation)
        self.assertEqual(free_bytes, self.suser.free_bytes)

    @defer.inlineCallbacks
    def test_get_delta_from_last(self):
        """Test that User.get_delta works as expected."""
        # create some nodes
        root = self.suser.root
        nodes = [root.make_file(u"name%s" % i) for i in range(5)]
        nodes += [root.make_subdirectory(u"dir%s" % i) for i in range(5)]
        from_generation = nodes[-1].generation
        delta, end_gen, free_bytes = yield self.user.get_delta(None,
                                                               from_generation)
        self.assertEqual(len(delta), 0)
        self.assertEqual(end_gen, nodes[-1].generation)
        self.assertEqual(free_bytes, self.suser.free_bytes)

    @defer.inlineCallbacks
    def test_get_delta_partial(self):
        """Test User.get_delta with partial delta."""
        # create some nodes
        root = self.suser.root
        nodes = [root.make_file(u"name%s" % i) for i in range(10)]
        nodes += [root.make_subdirectory(u"dir%s" % i) for i in range(10)]
        limit = 5
        delta, vol_gen, free_bytes = yield self.user.get_delta(None, 10,
                                                               limit=limit)
        self.assertEqual(len(delta), limit)
        self.assertEqual(vol_gen, 20)

    @defer.inlineCallbacks
    def test_rescan_from_scratch(self):
        """Test User.rescan_from_scratch."""
        root = self.suser.root
        nodes = [root.make_file(u"name%s" % i) for i in range(5)]
        nodes += [root.make_subdirectory(u"dir%s" % i) for i in range(5)]
        for f in [root.make_file(u"name%s" % i) for i in range(5, 10)]:
            f.delete()
        for d in [root.make_subdirectory(u"dir%s" % i) for i in range(5, 10)]:
            d.delete()
        live_nodes, gen, free_bytes = yield self.user.get_from_scratch(None)
        # nodes + root
        self.assertEqual(len(nodes) + 1, len(live_nodes))
        self.assertEqual(30, gen)


class TestContentManagerTests(TestWithDatabase):
    """Test ContentManger class."""

    @defer.inlineCallbacks
    def setUp(self):
        """Setup the test."""
        yield super(TestContentManagerTests, self).setUp()
        self.suser = self.make_user(max_storage_bytes=64 ** 2)
        self.cm = ContentManager(self.service.factory)
        self.cm.rpc_dal = self.service.rpc_dal

    @defer.inlineCallbacks
    def test_get_user_by_id(self):
        """Test get_user_by_id."""
        # user isn't cached yet.
        u = yield self.cm.get_user_by_id(self.suser.id)
        self.assertEqual(u, None)
        u = yield self.cm.get_user_by_id(self.suser.id, required=True)
        self.assertIsInstance(u, User)
        # make sure it's in the cache
        self.assertEqual(u, self.cm.users[self.suser.id])
        # get it from the cache
        u = yield self.cm.get_user_by_id(self.suser.id)
        self.assertIsInstance(u, User)

    @defer.inlineCallbacks
    def test_get_user_by_id_race_condition(self):
        """Two requests both try to fetch and cache the user."""
        # Has to fire before first call to rpc client returns
        d = defer.Deferred()
        rpc_call = self.cm.rpc_dal.call

        @defer.inlineCallbacks
        def delayed_rpc_call(funcname, **kwargs):
            """Wait for the deferred, then make the real client call."""
            yield d
            val = yield rpc_call(funcname, **kwargs)
            defer.returnValue(val)

        self.cm.rpc_dal.call = delayed_rpc_call

        # Start the first call
        u1_deferred = self.cm.get_user_by_id(self.suser.id, required=True)
        # Start the second call
        u2_deferred = self.cm.get_user_by_id(self.suser.id, required=True)
        # Let the first continue
        d.callback(None)
        # Get the results
        u1 = yield u1_deferred
        u2 = yield u2_deferred

        self.assertIdentical(u1, u2)
        self.assertIdentical(u1, self.cm.users[self.suser.id])


class TestContent(TestWithDatabase):
    """Test the upload and download."""

    def test_getcontent(self):
        """Get the content from a file."""
        data = "*" * 100000
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def check_file(req):
            if req.data != deflated_data:
                raise Exception("data does not match")

        def auth(client):
            d = client.dummy_authenticate("open sesame")
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, "hola"),
                client.test_fail)
            d.addCallback(self.save_req, 'req')
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(lambda _: client.get_content(
                          request.ROOT, self._state.req.new_id, hash_value))
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
        return self.callback_test(auth)

    def test_putcontent(self):
        """Test putting content to a file."""
        data = os.urandom(100000)
        deflated_data = zlib.compress(data)
        hash_object = content_hash_factory()
        hash_object.update(data)
        hash_value = hash_object.content_hash()
        crc32_value = crc32(data)
        size = len(data)
        deflated_size = len(deflated_data)

        def auth(client):

            def check_file(result):

                def _check_file():
                    try:
                        self.usr0.volume().get_content(hash_value)
                    except errors.DoesNotExist:
                        raise ValueError("content blob is not there")
                d = threads.deferToThread(_check_file)
                return d

            d = client.dummy_authenticate("open sesame")
            filename = 'hola'
            d.addCallbacks(lambda _: client.get_root(), client.test_fail)
            d.addCallbacks(
                lambda root: client.make_file(request.ROOT, root, filename),
                client.test_fail)
            d.addCallbacks(
                lambda mkfile_req: client.put_content(
                    request.ROOT, mkfile_req.new_id, NO_CONTENT_HASH,
                    hash_value, crc32_value, size, deflated_size,
                    StringIO(deflated_data)),
                client.test_fail)
            d.addCallback(check_file)
            d.addCallbacks(client.test_done, client.test_fail)
            return d

        return self.callback_test(auth)


class DBUploadJobTestCase(TestCase):
    """Tests for the DBUploadJob."""

    class FakeUser(object):
        """Fake object that simulates a rpc_dal call."""

        def __init__(self, to_return):
            self.to_return = to_return
            self.recorded = None
            self.id = 'fake_user_id'

        def call(self, method, **attribs):
            """Record the call."""
            self.recorded = (method, attribs)
            return defer.succeed(self.to_return)

        rpc_dal = property(lambda self: self)

    def setUp(self):
        """Set up."""
        d = dict(uploadjob_id='uploadjob_id', uploaded_bytes='uploaded_bytes',
                 multipart_key='multipart_key', chunk_count='chunk_count',
                 when_last_active='when_last_active')
        self.user = self.FakeUser(to_return=d)
        return super(DBUploadJobTestCase, self).setUp()

    @defer.inlineCallbacks
    def test_get(self):
        """Test the getter."""
        args = (self.user, 'volume_id', 'node_id', 'uploadjob_id',
                'hash_value', 'crc32')
        dbuj = yield DBUploadJob.get(*args)

        # check it called rpc dal correctly
        method, attribs = self.user.recorded
        self.assertEqual(method, 'get_uploadjob')
        should = dict(user_id='fake_user_id', volume_id='volume_id',
                      node_id='node_id', uploadjob_id='uploadjob_id',
                      hash_value='hash_value', crc32='crc32')
        self.assertEqual(attribs, should)

        # check it built the instance correctly
        self.assertIsInstance(dbuj, DBUploadJob)
        self.assertEqual(dbuj.user, self.user)
        self.assertEqual(dbuj.volume_id, 'volume_id')
        self.assertEqual(dbuj.node_id, 'node_id')
        self.assertEqual(dbuj.uploadjob_id, 'uploadjob_id')
        self.assertEqual(dbuj.uploaded_bytes, 'uploaded_bytes')
        self.assertEqual(dbuj.multipart_key, 'multipart_key')
        self.assertEqual(dbuj.chunk_count, 'chunk_count')
        self.assertEqual(dbuj.when_last_active, 'when_last_active')

    @defer.inlineCallbacks
    def test_make(self):
        """Test the builder."""
        args = (self.user, 'volume_id', 'node_id', 'previous_hash',
                'hash_value', 'crc32', 'inflated_size')
        self.patch(uuid, 'uuid4', lambda: "test unique id")
        dbuj = yield DBUploadJob.make(*args)

        # check it called rpc dal correctly
        method, attribs = self.user.recorded
        self.assertEqual(method, 'make_uploadjob')
        should = dict(user_id='fake_user_id', volume_id='volume_id',
                      node_id='node_id', previous_hash='previous_hash',
                      hash_value='hash_value', crc32='crc32',
                      inflated_size='inflated_size',
                      multipart_key='test unique id')
        self.assertEqual(attribs, should)

        # check it built the instance correctly
        self.assertIsInstance(dbuj, DBUploadJob)
        self.assertEqual(dbuj.user, self.user)
        self.assertEqual(dbuj.volume_id, 'volume_id')
        self.assertEqual(dbuj.node_id, 'node_id')
        self.assertEqual(dbuj.uploadjob_id, 'uploadjob_id')
        self.assertEqual(dbuj.uploaded_bytes, 'uploaded_bytes')
        self.assertEqual(dbuj.multipart_key, 'test unique id')
        self.assertEqual(dbuj.chunk_count, 'chunk_count')
        self.assertEqual(dbuj.when_last_active, 'when_last_active')

    def _make_uj(self):
        """Helper to create the upload job."""
        args = (self.user, 'volume_id', 'node_id', 'previous_hash',
                'hash_value', 'crc32', 'inflated_size')
        return DBUploadJob.make(*args)

    @defer.inlineCallbacks
    def test_add_part(self):
        """Test add_part method."""
        dbuj = yield self._make_uj()
        chunk_size = int(settings.STORAGE_CHUNK_SIZE) + 1
        yield dbuj.add_part(chunk_size)

        # check it called rpc dal correctly
        method, attribs = self.user.recorded
        self.assertEqual(method, 'add_part_to_uploadjob')
        should = dict(user_id='fake_user_id', uploadjob_id='uploadjob_id',
                      chunk_size=chunk_size, volume_id='volume_id')
        self.assertEqual(attribs, should)

    @defer.inlineCallbacks
    def test_delete(self):
        """Test delete method."""
        dbuj = yield self._make_uj()
        yield dbuj.delete()

        # check it called rpc dal correctly
        method, attribs = self.user.recorded
        self.assertEqual(method, 'delete_uploadjob')
        should = dict(user_id='fake_user_id', uploadjob_id='uploadjob_id',
                      volume_id='volume_id')
        self.assertEqual(attribs, should)

    @defer.inlineCallbacks
    def test_touch(self):
        """Test the touch method."""
        dbuj = yield self._make_uj()
        self.user.to_return = dict(when_last_active='new_when_last_active')
        yield dbuj.touch()

        # check it called rpc dal correctly
        method, attribs = self.user.recorded
        self.assertEqual(method, 'touch_uploadjob')
        should = dict(user_id='fake_user_id', uploadjob_id='uploadjob_id',
                      volume_id='volume_id')
        self.assertEqual(attribs, should)

        # check updated attrib
        self.assertEqual(dbuj.when_last_active, 'new_when_last_active')

    @defer.inlineCallbacks
    def test_bogus_upload_job(self):
        """Check the not-going-to-db upload job."""
        self.patch(uuid, 'uuid4', lambda: "test unique id")
        uj = BogusUploadJob()

        # basic attributes
        self.assertEqual(uj.multipart_key, "test unique id")
        self.assertEqual(uj.uploaded_bytes, 0)

        # check methods
        yield uj.add_part(123)
        yield uj.delete()
