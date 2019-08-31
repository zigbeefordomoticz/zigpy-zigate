from unittest import mock

import pytest
import serial_asyncio
from zigpy_zigate import api as zigate_api
import zigpy_zigate.types as t


@pytest.fixture
def api():
    api = zigate_api.ZiGate()
    api._uart = mock.MagicMock()
    return api


def test_set_application(api):
    api.set_application(mock.sentinel.app)
    assert api._app == mock.sentinel.app


@pytest.mark.asyncio
async def test_connect(monkeypatch):
    api = zigate_api.ZiGate()
    portmock = mock.MagicMock()

    async def mock_conn(loop, protocol_factory, **kwargs):
        protocol = protocol_factory()
        loop.call_soon(protocol.connection_made, None)
        return None, protocol
    monkeypatch.setattr(serial_asyncio, 'create_serial_connection', mock_conn)

    await api.connect(portmock, 115200)


def test_close(api):
    api._uart.close = mock.MagicMock()
    api.close()
    assert api._uart.close.call_count == 1


@pytest.mark.asyncio
async def test_remove_device(api):
    zigate_ieee = t.EUI64(b'\x12\x34\x56\x78\x9a\xbc\xde\xf0')
    ieee = t.EUI64(b'\x12\x34\x56\x78\x9a\xbc\xde\xf1')
    await api.remove_device(zigate_ieee, ieee)
