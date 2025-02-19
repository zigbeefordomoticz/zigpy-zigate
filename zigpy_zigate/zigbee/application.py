import asyncio
import logging
from typing import Any, Dict, Optional

import zigpy.application
import zigpy.config
import zigpy.device
import zigpy.types
import zigpy.util
import zigpy.zdo
import zigpy.exceptions

from zigpy_zigate import types as t
from zigpy_zigate import common as c
from zigpy_zigate.api import NoResponseError, ZiGate, PDM_EVENT
from zigpy_zigate.config import CONF_DEVICE, CONF_DEVICE_PATH, CONFIG_SCHEMA, SCHEMA_DEVICE, CONF_NWK, CONF_NWK_EXTENDED_PAN_ID

LOGGER = logging.getLogger(__name__)
ZDO_PROFILE = 0x0000
ZDO_ENDPOINT = 0


class ControllerApplication(zigpy.application.ControllerApplication):
    SCHEMA = CONFIG_SCHEMA
    SCHEMA_DEVICE = SCHEMA_DEVICE

    probe = ZiGate.probe

    def __init__(self, config: Dict[str, Any]):
        super().__init__(zigpy.config.ZIGPY_SCHEMA(config))
        self._api: Optional[ZiGate] = None

        self._pending = {}
        self._pending_join = []

        self.version = ''

    async def connect(self):
        api = await ZiGate.new(self._config[CONF_DEVICE], self)
        await api.set_raw_mode()
        await api.set_time()
        version, lqi = await api.version()

        hex_version = f"{version[1]:x}"
        self.version = f"{hex_version[0]}.{hex_version[1:]}"
        self._api = api

        if self.version < '3.21':
            LOGGER.warning('Old ZiGate firmware detected, you should upgrade to 3.21 or newer')

    async def disconnect(self):
        # TODO: how do you stop the network? Is it possible?
        await self._api.reset()
        
        if self._api:
            self._api.close()
            self._api = None

    async def start_network(self):
        # TODO: how do you start the network? Is it always automatically started?
        dev = ZiGateDevice(self, self.state.node_info.ieee, self.state.node_info.nwk)
        self.devices[dev.ieee] = dev


    async def load_network_info(self, *, load_devices: bool = False):
        network_state, lqi = await self._api.get_network_state()

        if not network_state or network_state[3] == 0 or network_state[0] == 0xffff:
            raise zigpy.exceptions.NetworkNotFormed()

        self.state.network_info = zigpy.state.NetworkInfo(
            extended_pan_id=zigpy.types.ExtendedPanId(network_state[3]),
            pan_id=network_state[2],
            nwk_update_id=None,
            nwk_manager_id=0x0000,
            channel=network_state[4],
            channel_mask=None,
            security_level=5,
            network_key=None,  # TODO: is it possible to read the network key?
            tc_link_key=None,
            children=[],
            key_table=[],
            nwk_addresses={},
            stack_specific=None,
        )
        
        self.state.node_info = zigpy.state.NodeInfo (
            nwk = zigpy.types.NWK(network_state[0]),
            ieee = zigpy.types.EUI64(network_state[1]),
            logical_type = None
        )

    async def write_network_info(self, *, network_info, node_info):
        LOGGER.warning('Setting the pan_id is not supported by ZiGate')

        await self._api.set_channel(network_info.channel)
        await self._api.set_extended_panid(network_info.extended_pan_id)

        network_formed, lqi = await self._api.start_network()
        if network_formed[0] in (0, 1, 4):
            return

        LOGGER.warning('Starting network got status %s, wait...', network_formed[0])
        for attempt in range(3):
            await asyncio.sleep(1)

            try:
                await self.load_network_info()
            except zigpy.exceptions.NetworkNotFormed as e:
                if attempt == 2:
                    raise zigpy.exceptions.FormationFailure() from e

    async def permit_with_key(self, node, code, time_s = 60):
        LOGGER.warning("ZiGate does not support joins with install codes")


    async def force_remove(self, dev):
        await self._api.remove_device(self.state.node_info.ieee, dev.ieee)


    def zigate_callback_handler(self, msg, response, lqi):
        LOGGER.debug('zigate_callback_handler {}'.format(response))

        if msg == 0x8048:  # leave
            nwk = 0
            ieee = zigpy.types.EUI64(response[0])
            self.handle_leave(nwk, ieee)
        elif msg == 0x004D:  # join
            if lqi != 0 :
                nwk = zigpy.types.NWK(response[0])
                ieee = zigpy.types.EUI64(response[1])
                parent_nwk = 0
                self.handle_join(nwk, ieee, parent_nwk)
                device = self.get_device(ieee=ieee)
                rssi = 0
                device.radio_details(lqi, rssi)
                data = t.uint8_t (0x00).serialize() #sqn 
                data += nwk.serialize() # nwk
                data += ieee.serialize() # ieee
                data += response[2].serialize() #mac cap

                self.handle_message(
                    device,
                    profile=ZDO_PROFILE,
                    cluster=0x0013,
                    src_ep=ZDO_ENDPOINT,
                    dst_ep=ZDO_ENDPOINT,
                    message=data ,
                )                
            # Temporary disable two stages pairing due to firmware bug
            # rejoin = response[3]
            # if nwk in self._pending_join or rejoin:
            #     LOGGER.debug('Finish pairing {} (2nd device announce)'.format(nwk))
            #     if nwk in self._pending_join:
            #         self._pending_join.remove(nwk)
            #     self.handle_join(nwk, ieee, parent_nwk)
            # else:
            #     LOGGER.debug('Start pairing {} (1st device announce)'.format(nwk))
            #     self._pending_join.append(nwk)
        elif msg == 0x8002:
            if response[1] == 0x0 and response[2] == 0x13:
                nwk = zigpy.types.NWK(response[5].address)
                ieee = zigpy.types.EUI64(response[7][3:11])
                parent_nwk = 0
                self.handle_join(nwk, ieee, parent_nwk)
                return
            try:
                if response[5].address_mode == t.ADDRESS_MODE.NWK:
                    device = self.get_device(nwk = zigpy.types.NWK(response[5].address))
                elif response[5].address_mode == t.ADDRESS_MODE.IEEE:
                    device = self.get_device(ieee=zigpy.types.EUI64(response[5].address))
                else:
                    LOGGER.error("No such device %s", response[5].address)
                    return
            except KeyError:
                LOGGER.debug("No such device %s", response[5].address)
                return
            rssi = 0
            device.radio_details(lqi, rssi)
            self.handle_message(device, response[1],
                                response[2],
                                response[3], response[4], response[-1])
        elif msg == 0x8011:  # ACK Data
            LOGGER.debug('ACK Data received %s %s', response[4], response[0])
            # disabled because of https://github.com/fairecasoimeme/ZiGate/issues/324
            # self._handle_frame_failure(response[4], response[0])
        elif msg == 0x8012:  # ZPS Event
            LOGGER.debug('ZPS Event APS data confirm, message routed to %s %s', response[3], response[0])
        elif msg == 0x8035:  # PDM Event
            try:
                event = PDM_EVENT(response[0]).name
            except ValueError:
                event = 'Unknown event'
            LOGGER.debug('PDM Event %s %s, record %s', response[0], event, response[1])
        elif msg == 0x8702:  # APS Data confirm Fail
            LOGGER.debug('APS Data confirm Fail %s %s', response[4], response[0])
            self._handle_frame_failure(response[4], response[0])
        elif msg == 0x9999:  # ZCL event
            LOGGER.warning('Extended error code %s', response[0])

    def _handle_frame_failure(self, message_tag, status):
        try:
            send_fut = self._pending.pop(message_tag)
            send_fut.set_result(status)
        except KeyError:
            LOGGER.warning("Unexpected message send failure")
        except asyncio.futures.InvalidStateError as exc:
            LOGGER.debug("Invalid state on future - probably duplicate response: %s", exc)

    @zigpy.util.retryable_request
    async def request(self, device, profile, cluster, src_ep, dst_ep, sequence, data,
                      expect_reply=True, use_ieee=False):
        if device.nwk is not None:
            return await self._request(device.nwk, profile, cluster, src_ep, dst_ep, sequence, data,expect_reply, use_ieee, addr_mode=2)
        elif device.ieee is not None:
            return await self._request(device.ieee, profile, cluster, src_ep, dst_ep, sequence, data,expect_reply, use_ieee, addr_mode=3)

    async def mrequest(self, group_id, profile, cluster, src_ep, sequence, data, *, hops=0, non_member_radius=3):
        src_ep = 1
        return await self._request(group_id, profile, cluster, src_ep, src_ep, sequence, data, addr_mode=1)

    async def broadcast(
        self,
        profile,
        cluster,
        src_ep,
        dst_ep,
        grpid,
        radius,
        sequence,
        data,
        broadcast_address=zigpy.types.BroadcastAddress.RX_ON_WHEN_IDLE,
    ) :
       return await self._request(broadcast_address, profile, cluster, src_ep, src_ep, sequence, data, expect_reply=False, addr_mode=4)

    async def _request(self, nwk, profile, cluster, src_ep, dst_ep, sequence, data,
                      expect_reply=True, use_ieee=False, addr_mode=2):
        LOGGER.debug('request %s',
                     (nwk, profile, cluster, src_ep, dst_ep, sequence, data, expect_reply, use_ieee,addr_mode,expect_reply))
        try:
            v, lqi = await self._api.raw_aps_data_request(nwk, src_ep, dst_ep, profile, cluster, data, addr_mode,expect_reply=expect_reply)
        except NoResponseError:
            return 1, "ZiGate doesn't answer to command"
        req_id = v[1]
        send_fut = asyncio.Future()
        self._pending[req_id] = send_fut

        if v[0] != 0:
            self._pending.pop(req_id)
            return v[0], "Message send failure {}".format(v[0])

        # disabled because of https://github.com/fairecasoimeme/ZiGate/issues/324
        # try:
        #     v = await asyncio.wait_for(send_fut, 120)
        # except asyncio.TimeoutError:
        #     return 1, "timeout waiting for message %s send ACK" % (sequence, )
        # finally:
        #     self._pending.pop(req_id)
        # return v, "Message sent"
        return 0, "Message sent"

    async def permit_ncp(self, time_s=60):
        assert 0 <= time_s <= 254
        status, lqi = await self._api.permit_join(time_s)
        if status[0] != 0:
            await self._api.reset()


class ZiGateDevice(zigpy.device.Device):
    def __init__(self, application, ieee, nwk):
        """Initialize instance."""

        super().__init__(application, ieee, nwk)
        port = application._config[CONF_DEVICE][CONF_DEVICE_PATH]
        model = 'ZiGate USB-TTL'
        if c.is_zigate_wifi(port):
            model = 'ZiGate WiFi'
        elif c.is_pizigate(port):
            model = 'PiZiGate'
        elif c.is_zigate_din(port):
            model = 'ZiGate USB-DIN'
        self._model = '{} {}'.format(model, application.version)

    @property
    def manufacturer(self):
        return "ZiGate"

    @property
    def model(self):
        return self._model
