import sys
import threading

import appdaemon.plugins.hass.hassapi as hass

import paho.mqtt.client as mqtt
from pyarlo import PyArlo

PACKAGES = ['paho-mqtt', 'PyArlo']

"""

Netgear Arlo Alarm Control Panel.

Replacement for Home Assistant's Arlo alarm control panel, using the MQTT alarm control panel instead.

Args:
arlo.username - Arlo username
arlo.password - Arlo password
mqtt.host - MQTT host
mqtt.port - MQTT port


"""
class ArloAlarmControlPanel(hass.Hass):
    def initialize(self):
        self.log('Starting Arlo Alarm Control Panel...')

        config_arlo = self.args['arlo']
        config_mqtt = self.args['mqtt']

        self.pending_time = self.args['pending_time'] if 'pending_time' in self.args else 0
        self.state_topic = self.args['state_topic'] if 'state_topic' in self.args else 'home/alarm'
        self.command_topic = self.args['command_topic'] if 'command_topic' in self.args else 'home/alarm/set'
        self.availability_topic = self.args['availability_topic'] if 'availability_topic' in self.args else 'home/alarm/availability'

        # Connect to Arlo
        self.lock = threading.Lock()
        self.arlo = PyArlo(config_arlo['username'], config_arlo['password'], preload=False)

        self.base = self.arlo.base_stations[0]

        self.previous_mode = 'unknown'
        self.pending_command = None
        self.handle = self.run_in(self.update_state, 1)

        # Setup MQTT Client
        self.client = mqtt.Client(userdata=self.base.name)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        self.client.will_set(self.availability_topic, 'offline', qos=1, retain=True)

        if 'username' in config_mqtt:
            self.client.username_pw_set(config_mqtt['username'], password=config_mqtt['password'])
        self.client.connect_async(config_mqtt['host'], config_mqtt['port'], 60)

        self.client.loop_start()

    def terminate(self):
        self.log('Terminating Arlo Alarm Control Panel')
        self.lock.acquire()
        try:
            del self.arlo
            self.arlo = None

            self.client.loop_stop()
            self.client.disconnect()
            del self.client
        finally:
            self.lock.release()

    def on_connect(self, client, userdata, flags, rc):
        self.log(f'[{userdata}] MQTT client Connected with result code {rc}')

        client.subscribe(self.command_topic, qos=1)
        client.publish(self.availability_topic, 'online', qos=1, retain=True)

    def on_disconnect(self, client, userdata, flags):
        self.log(f'[{userdata}] MQTT client disconnected')

    def on_message(self, client, userdata, msg):
        self.cancel_timer(self.handle)
        self.cancel_timer(self.pending_command)

        if msg.payload == b'DISARM':
            self.client.publish(self.state_topic, 'pending', qos=1, retain=False)
            self.pending_command = self.run_in(self._set_alarm_mode, self._pending_time('disarmed'), mode='disarmed')
        elif msg.payload == b'ARM_AWAY':
            self.client.publish(self.state_topic, 'pending', qos=1, retain=False)
            self.pending_command = self.run_in(self._set_alarm_mode, self._pending_time('armed_away'), mode='armed')
        else:
            self.log(f'[{userdata}] MQTT: {msg.topic} {msg.payload} (qos={msg.qos})')

        self.handle = self.run_in(self.update_state, 1)

    def _pending_time(self, mode):
        if mode in self.args and 'pending_time' in self.args[mode]:
            return self.args[mode]['pending_time']

        return self.pending_time

    def _set_alarm_mode(self, kwargs):
        self.lock.acquire()
        try:
            if self.arlo == None:
                return

            mode = kwargs['mode']

            self.log(f'[{self.base.name}] Setting mode to {mode}')
            self.base.publish(
                action='set',
                resource='modes',
                mode=mode,
                publish_response=True)
            self.previous_mode = 'pending'
        finally:
            self.lock.release()

    def update_state(self, kwargs):
        self.lock.acquire()
        try:
            if self.arlo == None:
                return

            mode = self.base.mode
            if mode == None:
                self.log('Detected stale connection. Re-authenticating.')
                try:
                    self.arlo.login()
                except:
                    e = sys.exc_info()[0]
                    self.log(e)
                    raise
            elif mode == 'armed':
                mode = 'armed_away'

            if mode != None and self.previous_mode != mode:
                self.log(f'[{self.base.name}] New alarm mode is {mode}')
                self.previous_mode = mode

                res = self.client.publish(self.state_topic, mode, qos=1, retain=True)
                res.wait_for_publish()

            self.handle = self.run_in(self.update_state, 1)
        finally:
            self.lock.release()