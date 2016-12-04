"""
 Working on setting zwave lock codes and storing name references for entries
"""
import logging
import collections
import os.path

import homeassistant.helpers.config_validation as cv
import homeassistant.components.zwave.const as zconst
import homeassistant.config as conf_util
from homeassistant.components import persistent_notification
from homeassistant.components import logbook
from homeassistant.util.yaml  import load_yaml, dump


import voluptuous as vol
from pydispatch import dispatcher
from openzwave.network import ZWaveNetwork


_LOGGER = logging.getLogger(__name__)
DEPENDENCIES = ['zwave']
DOMAIN = 'be369group'
PERSIST_FILE = 'lockinfo.yaml'
LOCKGROUP = None

USER_CODE_ENTERED = 16
TOO_MANY_FAILED_ATTEMPTS = 96
NOT_USER_CODE_INDEXES = (0, 254, 255)  # Enrollment code, refresh and code count
USER_CODE_STATUS_BYTE = 8


def setup(hass, config):
    """ Thanks to pydispatcher being globally available in the application, we can hook into zwave here """
    global LOCKGROUP
    LOCKGROUP = BE369LockGroup(hass) 
    dispatcher.connect(LOCKGROUP.valueadded, ZWaveNetwork.SIGNAL_VALUE_ADDED)
    dispatcher.connect(LOCKGROUP.valuechanged, ZWaveNetwork.SIGNAL_VALUE_CHANGED)

    hass.services.register(DOMAIN, "setusercode", LOCKGROUP.setcode,
                { 'description': "Set the user code at an index on all locks",
                       'fields': { 'index': {'description': 'The index (1-19)'},
                                    'name': {'description': 'A name for reference'},
                                   'value': {'description': 'The number code to use as ascii'}}})
    hass.services.register(DOMAIN, "clearusercode", LOCKGROUP.clearcode,
                { 'description': "Clear the user code at an index on all locks",
                       'fields': { 'index': {'description': 'The index (1-19)'}}})

    return True


class BE369LockGroup:
    """
        Group all the locks info together so we can set the same code in the same slot on each lock.  The Schlage
        locks do not let you download the user codes and I really don't care to store them, I just want to assign
        a name to each entry location (a la Vera handling) so I can remember which ones to delete/reassign later.

        I think this BE369 use of alarms is entirely specific to this old lock, there appears to be an actual
        lock logging class for newer zwave devices.  Ah, corner cases, my old friend. The default HA ZWaveAlarmSensor
        treats zwave alarms as separate state values and therefore won't send updates if the same door code entered
        multiple times in succession, hence this little snippet.

        We also have to resort to ugliness to get the data about UserCode availability.

        This has become pretty much all specific case work.
    """

    def __init__(self, hass):
        self.hass   = hass
        self.values = collections.defaultdict(list)  # index  -> [user code Value for each lock at index]
        self.alarms = dict()                         # nodeid -> [alarmtype, alarmval]
        self.names  = dict()                         # index  -> string to describe this entry
        self.load_name_info()


    def load_name_info(self):
        """ Load lock code name information after restart """
        try:
            self.names = load_yaml(self.hass.config.path(PERSIST_FILE))
            _LOGGER.debug("read in state: {}".format(self.names))
        except FileNotFoundError:
            pass # we'll create a new one eventually
        except Exception as e:
            _LOGGER.warning("error loading {}: {}".format(PERSIST_FILE, e))


    def store_name_info(self):
        """ Rewrite the lock code name information """
        with open(self.hass.config.path(PERSIST_FILE), 'w') as out:
            out.write("# Autogenerated file to survive data across restarts, I wouldn't recommend editing\n")
            out.write(dump(self.names))


    def setcode(self, service):
        """ Set the ascii number string code to index X on each available lock """
        index = service.data.get('index')
        name  = service.data.get('name')
        value = service.data.get('value')

        if not all([ord(x) in range(0x30, 0x39) for x in value]):
            _LOGGER.warning("Invalid code provided to setcode ({})".format(value))
            return
        for v in self.values[index]:
            v.data = value

    def clearcode(self, service):
        """ Clear the code at index X on each available lock """
        index = service.data.get('index')
        for v in self.values[index]:
            v.data = "\0\0\0\0"  # My patch to OZW should cause a clear


    def lockcodestatus(self, value):
        thelist = [v.available for v in self.values[value.index]]
        theset  = set(thelist)

        if None in theset: # Not all codes at this index refreshed for all locks
            pass

        elif len(theset) != 1: # differing ideas on availability, uh oh
            _LOGGER.warning("Locks disagree on availability of index {}. set = {}".format(value.index, theset))

        elif thelist[0]:  # Available
            if value.index in self.names: # we have a name for something that should be available, clear the name
                del self.names[value.index]
                self.store_name_info()

        else: # Occupied
            if value.index not in self.names: # we should have a name but we don't, add temp name
                self.names[value.index] = "Unamed Entry {}".format(value.index)
                self.store_name_info()


    def lockactivity(self, nodeid, atype, aval):
        """ We have decoded a report (via alarms) from the BE369 lock """
        if atype == USER_CODE_ENTERED:
            logbook.log_entry(self.hass, "LockNameHere", 'User entered door code (node={}, slot={})'.format(nodeid, aval))

        elif atype == TOO_MANY_FAILED_ATTEMPTS:
            msg = 'Multiple invalid door codes enetered at node {}'.format(nodeid)
            persistent_notification.create(self.hass, msg, 'Potential Prowler')
            _LOGGER.warning(msg)

        else:
            _LOGGER.warning("Unknown lock alarm type! Investigate ({}, {}, {})".format(nodeid, atype, aval))


    def valueadded(self, node, value):
        """ New ZWave Value added (generally on network start), make note of any user code entries on generic locks """
        if (node.generic == zconst.GENERIC_TYPE_ENTRY_CONTROL and     # node is generic lock
            value.command_class == zconst.COMMAND_CLASS_USER_CODE and # command class is user code
            value.index not in NOT_USER_CODE_INDEXES):                # real user code, not other indexes

            _LOGGER.debug("registered user code location {}, {} on {}".format(value.index, value.label, value.parent_id))
            value.available = None  # unknown third state
            self.values[value.index].append(value)
            self.alarms[value.parent_id] = [None]*2


    def valuechanged(self, value):
        """ We look for usercode and alarm messages from our locks here """

        if value.command_class == zconst.COMMAND_CLASS_USER_CODE:
            # PyOZW doesn't expose command class data, we reach into the raw message data and get it ourselves
            value.available = not value.network.manager.getNodeStatistics(value.home_id, value.parent_id)['lastReceivedMessage'][USER_CODE_STATUS_BYTE]
            _LOGGER.debug("{} code {} available {}".format(value.parent_id, value.index, value.available))
            self.lockcodestatus(value)


        elif value.command_class == zconst.COMMAND_CLASS_ALARM and value.parent_id in self.alarms:
            # When we get both COMMAND_CLASS_ALARM index 0 and index 1 from a Lock, we combine, process and reset the data
            _LOGGER.debug("alarm piece {} {} on {}".format(value.index, value.data, value.parent_id))
            try:
                bits = self.alarms[value.parent_id]
                bits[value.index] = value.data
                if None not in bits:
                    self.lockactivity(value.parent_id, bits[0], bits[1])
                    bits[:] = [None]*2

            except Exception as e:
                _LOGGER.error("exception {}: got bad data? index={}, data={}".format(e, value.index, value.data))

