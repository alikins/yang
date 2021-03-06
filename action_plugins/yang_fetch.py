# (c) 2018, Ansible by Red Hat, inc
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import re
import sys

is_py2 = sys.version[0] == '2'
if is_py2:
    import Queue as queue
else:
    import queue as queue

from ansible import constants as C
from ansible.plugins.action import ActionBase
from ansible.module_utils._text import to_bytes, to_text
from ansible.module_utils.connection import Connection, ConnectionError
from ansible.errors import AnsibleError

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()
try:
    from lxml import etree
    HAS_XML = True
except ImportError:
    HAS_XML = False

try:
    from lxml.etree import tostring
except ImportError:
    from xml.etree.ElementTree import tostring

try:
    import jxmlease
    HAS_JXMLEASE = True
except ImportError:
    HAS_JXMLEASE = False


def warning(msg):
    if C.ACTION_WARNINGS:
        display.warning(msg)


class SchemaStore(object):
    def __init__(self, conn):
        self._conn = conn
        self._schema_cache = []
        self._all_schema_list = None

    def get_schema_description(self):
        content = '''
          <filter>
            <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
              <schemas>
                <schema>
                    <identifier/>
                </schema>
              </schemas>
            </netconf-state>
          </filter>
        '''
        xml_request = '<%s>%s</%s>' % ('get', content, 'get')
        try:
            response = self._conn.dispatch(xml_request)
        except ConnectionError as e:
            raise ValueError(to_text(e))
        response = to_bytes(response, errors='surrogate_or_strict')
        tree = etree.ElementTree(etree.fromstring(response))
        tree_root = tree.getroot()
        res_str = etree.tostring(tree_root, pretty_print=True)

        if not HAS_JXMLEASE:
            raise ValueError('jxmlease is required to store response in json format'
                             'but does not appear to be installed. '
                             'It can be installed using `pip install jxmlease`')
        res_json = jxmlease.parse(res_str)
        self._all_schema_list = res_json["data"]["netconf-state"]["schemas"]["schema"]
        return

    def get_one_schema(self, schema_id, result):
        if self._all_schema_list is None:
            self.get_schema_description()

        found = False
        data_model = None
        # Search for schema that are supported by device.
        # Also get namespace for retrieval
        schema_cache_entry = {}
        for index, schema_list in enumerate(self._all_schema_list):
            if to_bytes(schema_id) == to_bytes(schema_list["identifier"],
                                               errors='surrogate_or_strict'):
                schema_cache_entry["id"] = to_bytes(schema_id,
                                                    errors='surrogate_or_strict')
                schema_cache_entry["ns"] = self._all_schema_list[index]["namespace"]
                schema_cache_entry["format"] = self._all_schema_list[index]["format"]
                found = True
                break

        if found:
            content = ("<identifier> %s </identifier>" % (schema_cache_entry["id"]))
            xmlns = "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"
            xml_request = '<%s xmlns="%s"> %s </%s>' % ('get-schema', xmlns,
                                                        content, 'get-schema')
            try:
                response = self._conn.dispatch(xml_request)
            except ConnectionError as e:
                raise ValueError(to_text(e))
            response = to_bytes(response, errors='surrogate_or_strict')
            tree = etree.ElementTree(etree.fromstring(response))
            tree_root = tree.getroot()
            res_str = etree.tostring(tree_root, pretty_print=True)

            if not HAS_JXMLEASE:
                raise ValueError('jxmlease is required to store response in json format'
                                 'but does not appear to be installed. '
                                 'It can be installed using `pip install jxmlease`')
            res_json = jxmlease.parse(res_str)
            data_model = res_json["rpc-reply"]["data"]
            display.vvv("Fetched '%s' yang model" % schema_id)
            result['fetched'][schema_id] = data_model
            self._schema_cache.append(schema_cache_entry)
        else:
            raise AnsibleError("Fail to fetch '%s' yang model" % schema_id)

        return found, data_model

    def get_schema_and_dependants(self, schema_id, result):
        try:
            found, data_model = self.get_one_schema(schema_id, result)
        except ValueError as exc:
            raise ValueError(exc)

        if found:
            result['fetched'][schema_id] = data_model
            importre = re.compile(r'import (.+) {')
            return importre.findall(data_model)
        else:
            return []

    def run(self, schema_id, result):
        changed = False
        counter = 1
        sq = queue.Queue()
        sq.put(schema_id)

        while sq.empty() is not True:
            schema_id = sq.get()
            if schema_id in result['fetched']:
                counter -= 1
                continue

            schema_dlist = self.get_schema_and_dependants(schema_id, result)
            for schema_id in schema_dlist:
                if schema_id not in result['fetched']:
                    sq.put(schema_id)
                    changed = True
                    counter += 1

        return changed, counter


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super(ActionModule, self).run(tmp, task_vars)

        try:
            schema = self._task.args['schema']
        except KeyError as exc:
            return {'failed': True, 'msg': 'missing required argument: %s' % exc}

        socket_path = self._connection.socket_path
        conn = Connection(socket_path)

        ss = SchemaStore(conn)

        result['fetched'] = dict()
        try:
            changed, counter = ss.run(schema, result)
        except ValueError as exc:
            return {'failed': True, 'msg': to_text(exc)}

        result["changed"] = changed
        result["number_schema_fetched"] = counter
        return result
