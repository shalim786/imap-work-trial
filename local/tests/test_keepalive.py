"""Keepalive transport settings and safe idle response boundaries."""
import asyncio
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock
from agentmail_imap.config import Config
from agentmail_imap.server import Server, Session
from test_session import Writer, Store


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server=Server(Config(temp_dir=Path(self.directory.name),heartbeat_interval=.01),Store())
        self.addAsyncCleanup(self.server.close)
        self.writer=Writer()
        self.session=Session(self.server,asyncio.StreamReader(),self.writer)

    async def test_socket_keepalive_enabled_with_platform_timers(self):
        sock=Mock()
        writer=Mock()
        writer.get_extra_info.return_value=sock
        self.server.configure_keepalive(writer)
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        idle=getattr(socket,'TCP_KEEPIDLE',getattr(socket,'TCP_KEEPALIVE',None))
        if idle is not None:
            sock.setsockopt.assert_any_call(socket.IPPROTO_TCP,idle,60)

    async def test_accepted_socket_has_keepalive(self):
        from dataclasses import replace
        self.server.config=replace(self.server.config,port=0)
        listener=await self.server.start()
        port=listener.sockets[0].getsockname()[1]
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
        try:
            self.assertTrue((await reader.readline()).startswith(b'* OK'))
            session=next(iter(self.server.sessions))
            sock=session.writer.get_extra_info('socket')
            self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE),0)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_heartbeat_does_not_enter_busy_response(self):
        self.session.busy=True
        task=asyncio.create_task(self.session.heartbeat())
        try:
            await asyncio.sleep(.04)
            self.assertEqual(bytes(self.writer.data),b'')
            self.session.busy=False
            await asyncio.sleep(.03)
            self.assertIn(b'* OK Keepalive\r\n',bytes(self.writer.data))
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_heartbeat_respects_literal_writer_lock(self):
        async with self.session.write_lock:
            task=asyncio.create_task(self.session.heartbeat())
            self.writer.write(b'* 1 FETCH (BODY[] {4}\r\n')
            await asyncio.sleep(.04)
            self.writer.write(b'test)\r\n')
            self.assertEqual(bytes(self.writer.data),b'* 1 FETCH (BODY[] {4}\r\ntest)\r\n')
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertIn(b'test)\r\n* OK Keepalive\r\n',bytes(self.writer.data))

    async def test_idle_timeout_closes_without_leaking_heartbeat(self):
        from dataclasses import replace
        self.server.config=replace(self.server.config,idle_timeout=.03)
        await self.session.run()
        self.assertTrue(self.writer.closed)
        length=len(self.writer.data)
        await asyncio.sleep(.03)
        self.assertEqual(len(self.writer.data),length)

    def test_keepalive_config_rejects_nonpositive(self):
        for field in ('heartbeat_interval','tcp_keepalive_idle','tcp_keepalive_interval','tcp_keepalive_count'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                Config(**{field:0})
