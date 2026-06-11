#!/usr/bin/env python3
"""
StreamTouch UPnP Shim
Bridges Music Assistant content to SoundTouch hardware presets
via a local UPnP/DLNA MediaServer and BMX registry
"""

import json
import os
import uuid
import threading
import requests
import logging
import re
import socket
import time
import base64
from flask import Flask, request, Response, jsonify
from zeroconf import ServiceInfo, Zeroconf

# ─── Configuration ────────────────────────────────────────────────────────────

MA_HOST = os.environ.get("MA_HOST", "192.168.0.253")
MA_PORT = os.environ.get("MA_PORT", "8095")
MA_TOKEN = os.environ.get("MA_TOKEN", "")
SHIM_HOST = os.environ.get("SHIM_HOST", "192.168.0.254")
SHIM_PORT = int(os.environ.get("SHIM_PORT", "8300"))
SHIM_UUID = os.environ.get("SHIM_UUID", "streamtouch-upnp-0000-0000-000000000001")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")

# Fixed token — ST10/Wave store this after device registration
SHIM_TOKEN = "bst_streamtouchlocalaccesstoken00001"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger("streamtouch-upnp")

app = Flask(__name__)

# ─── In-memory recent store ───────────────────────────────────────────────────
# Keyed by device_id → {recent_id: {name, location, contentItemType, lastplayedat}}
# Drives LED display on Wave/ST10
recent_store = {}

# ─── Preset storage ───────────────────────────────────────────────────────────

def load_presets():
    if os.path.exists(PRESETS_FILE):
        with open(PRESETS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_presets(presets):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PRESETS_FILE, "w") as f:
        json.dump(presets, f, indent=2)

# ─── UPnP XML definitions ─────────────────────────────────────────────────────

def get_device_description():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>1</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>StreamTouch Music Assistant</friendlyName>
    <manufacturer>StreamTouch</manufacturer>
    <manufacturerURL>https://github.com/streamtouch</manufacturerURL>
    <modelDescription>StreamTouch UPnP Bridge for Music Assistant</modelDescription>
    <modelName>StreamTouch UPnP Shim</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>uuid:{SHIM_UUID}</UDN>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/ContentDirectory/{SHIM_UUID}/scpd.xml</SCPDURL>
        <controlURL>/ContentDirectory/{SHIM_UUID}/control.xml</controlURL>
        <eventSubURL>/ContentDirectory/{SHIM_UUID}/event.xml</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager/{SHIM_UUID}/scpd.xml</SCPDURL>
        <controlURL>/ConnectionManager/{SHIM_UUID}/control.xml</controlURL>
        <eventSubURL>/ConnectionManager/{SHIM_UUID}/event.xml</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""

CONTENT_DIRECTORY_SCPD = """<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name>
      <dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name>
      <dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name>
      <dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name>
      <dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>ContainerUpdateIDs</name>
      <dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

CONNECTION_MANAGER_SCPD = """<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction>
          <relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction>
          <relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name>
      <dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name>
      <dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name>
      <dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name>
      <dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

# ─── XML helpers ──────────────────────────────────────────────────────────────

def escape_xml(text):
    return (str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;"))

def build_didl_container(object_id, parent_id, title, child_count=0):
    return (
        f'<container id="{object_id}" parentID="{parent_id}" '
        f'restricted="1" searchable="0" childCount="{child_count}">'
        f'<dc:title>{escape_xml(title)}</dc:title>'
        f'<upnp:class>object.container.storageFolder</upnp:class>'
        f'</container>'
    )

def build_didl_item(
    object_id, parent_id, title, stream_url, artwork_url=None
):
    art = (
        f'<upnp:albumArtURI>{escape_xml(artwork_url)}</upnp:albumArtURI>'
        if artwork_url else ''
    )
    return (
        f'<item id="{object_id}" parentID="{parent_id}" restricted="1">'
        f'<dc:title>{escape_xml(title)}</dc:title>'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'{art}'
        f'<res protocolInfo="http-get:*:audio/mpeg:*">'
        f'{escape_xml(stream_url)}</res>'
        f'</item>'
    )

def build_soap_browse_response(
    didl_content, number_returned, total_matches
):
    didl = (
        f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        f'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        f'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        f'{didl_content}'
        f'</DIDL-Lite>'
    )
    escaped_didl = escape_xml(didl)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"
            xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <Result>{escaped_didl}</Result>
      <NumberReturned>{number_returned}</NumberReturned>
      <TotalMatches>{total_matches}</TotalMatches>
      <UpdateID>1</UpdateID>
    </u:BrowseResponse>
  </s:Body>
</s:Envelope>"""

# ─── Recent XML builder ───────────────────────────────────────────────────────

def build_recent_xml(recent_id, item):
    """Build a full recent XML response including name for LED display."""
    now = "2026-01-01T00:00:00.000+00:00"
    name = escape_xml(item.get("name", ""))
    location = escape_xml(item.get("location", ""))
    content_type = escape_xml(
        item.get("contentItemType", "stationurl")
    )
    last_played = item.get("lastplayedat", now)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<recent id="{recent_id}">\n'
        f'  <contentItemType>{content_type}</contentItemType>\n'
        f'  <createdOn>{now}</createdOn>\n'
        f'  <lastplayedat>{last_played}</lastplayedat>\n'
        f'  <location>{location}</location>\n'
        f'  <name>{name}</name>\n'
        f'  <source id="ST_LIR_001" type="Audio">\n'
        f'    <createdOn>{now}</createdOn>\n'
        f'    <credential type="token">'
        f'streamtouch-lir-token</credential>\n'
        f'    <name></name>\n'
        f'    <sourceproviderid>11</sourceproviderid>\n'
        f'    <sourcename>LOCAL_INTERNET_RADIO</sourcename>\n'
        f'    <sourceSettings/>\n'
        f'    <updatedOn>{now}</updatedOn>\n'
        f'    <username></username>\n'
        f'  </source>\n'
        f'  <sourceid>ST_LIR_001</sourceid>\n'
        f'  <updatedOn>{now}</updatedOn>\n'
        f'</recent>'
    )

# ─── MA stream URL resolver ───────────────────────────────────────────────────

def get_ma_stream_url(ma_uri):
    try:
        headers = {
            "Authorization": f"Bearer {MA_TOKEN}",
            "Content-Type": "application/json"
        }
        resp = requests.post(
            f"http://{MA_HOST}:{MA_PORT}/api",
            json={
                "command": "music/get_stream_url",
                "args": {"uri": ma_uri}
            },
            headers=headers,
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, str) and data.startswith("http"):
                log.info(f"Stream URL from MA: {data}")
                return data
        if ma_uri.startswith("http"):
            log.info(f"Using direct URL: {ma_uri}")
            return ma_uri
        log.warning(f"Could not resolve stream URL for: {ma_uri}")
        return None
    except Exception as e:
        log.error(f"Error resolving stream URL: {e}")
        return None

# ─── Request logger ───────────────────────────────────────────────────────────

@app.before_request
def log_request():
    ua = request.headers.get('User-Agent', 'unknown')
    log.info(
        f"← {request.method} {request.path} "
        f"from {request.remote_addr} UA={ua}"
    )

# ─── Root probe ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    """ST10/Wave connectivity probe."""
    log.info(f"Root probe from {request.remote_addr}")
    return Response(
        '{"status":"ok","service":"streamtouch-shim"}',
        status=200,
        mimetype="application/json"
    )

# ─── BMX Registry ─────────────────────────────────────────────────────────────

def build_registry_response():
    return jsonify({
        "_links": {
            "bmx_services_availability": {
                "href": "../servicesAvailability"
            }
        },
        "askAgainAfter": 86400,
        "bmx_services": [
            {
                "_links": {
                    "bmx_availability": {"href": "/availability"},
                    "bmx_token": {"href": "/token"},
                    "self": {"href": "/"}
                },
                "askAdapter": False,
                "assets": {
                    "color": "#000000",
                    "description": "StreamTouch Radio",
                    "name": "StreamTouch Radio"
                },
                "authenticationModel": {
                    "anonymousAccount": {
                        "autoCreate": True,
                        "enabled": True
                    }
                },
                "baseUrl": f"http://{SHIM_HOST}:{SHIM_PORT}/orion",
                "id": {
                    "name": "LOCAL_INTERNET_RADIO",
                    "value": 11
                },
                "streamTypes": ["liveRadio"]
            }
        ]
    })


def build_services_availability_response():
    return jsonify({
        "available": True,
        "services": ["LOCAL_INTERNET_RADIO"]
    })


@app.route("/registry.json")
def registry_legacy():
    log.info(f"Registry (legacy) from {request.remote_addr}")
    return build_registry_response()


@app.route("/bmx/registry/v1/services")
def registry_v1():
    log.info(f"Registry (v1) from {request.remote_addr}")
    return build_registry_response()


@app.route("/servicesAvailability")
def services_availability_legacy():
    log.info(
        f"ServicesAvailability (legacy) from {request.remote_addr}"
    )
    return build_services_availability_response()


@app.route("/bmx/registry/v1/servicesAvailability")
def services_availability_v1():
    log.info(
        f"ServicesAvailability (v1) from {request.remote_addr}"
    )
    return build_services_availability_response()

# ─── Streaming API ────────────────────────────────────────────────────────────

@app.route("/streaming/support/power_on", methods=["GET", "POST"])
def streaming_power_on():
    body = request.data.decode("utf-8", errors="ignore")
    device_id_match = re.search(r'<device\s+id="([^"]+)"', body)
    device_id = device_id_match.group(1) if device_id_match else ""
    log.info(
        f"Power on from {request.remote_addr} device={device_id}"
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<status>success</status>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route("/streaming/sourceproviders", methods=["GET"])
def streaming_source_providers():
    log.info(f"Source providers from {request.remote_addr}")
    response_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<sourceProviders>\n'
        '  <sourceprovider id="7">\n'
        '    <createdOn>2012-10-22T16:04:00.000+00:00</createdOn>\n'
        '    <name>STORED_MUSIC</name>\n'
        '    <updatedOn>2012-10-22T16:04:00.000+00:00</updatedOn>\n'
        '  </sourceprovider>\n'
        '  <sourceprovider id="11">\n'
        '    <createdOn>2013-01-10T09:45:00.000+00:00</createdOn>\n'
        '    <name>LOCAL_INTERNET_RADIO</name>\n'
        '    <updatedOn>2013-01-10T09:45:00.000+00:00</updatedOn>\n'
        '  </sourceprovider>\n'
        '</sourceProviders>'
    )
    return Response(
        response_xml,
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/full",
    methods=["GET"]
)
def streaming_account_full(account_id):
    """
    Full account response.
    Includes recents from store so LED display works correctly.
    sourceproviderid=11 activates LOCAL_INTERNET_RADIO.
    """
    auth = request.headers.get("Authorization", "none")
    log.info(
        f"Account full: id={account_id} "
        f"auth={auth[:40]} from {request.remote_addr}"
    )
    now = "2026-01-01T00:00:00.000+00:00"

    # Build devices XML with recents from store
    devices_xml = ""
    if recent_store:
        for device_id, device_recents in recent_store.items():
            recents_items_xml = ""
            for rid, item in list(device_recents.items())[-5:]:
                name = escape_xml(item.get("name", ""))
                last_played = item.get("lastplayedat", now)
                location = escape_xml(item.get("location", ""))
                ctype = escape_xml(
                    item.get("contentItemType", "stationurl")
                )
                recents_items_xml += (
                    f'      <recent id="{rid}">\n'
                    f'        <contentItemType>{ctype}</contentItemType>\n'
                    f'        <createdOn>{now}</createdOn>\n'
                    f'        <lastplayedat>{last_played}</lastplayedat>\n'
                    f'        <location>{location}</location>\n'
                    f'        <name>{name}</name>\n'
                    f'        <source id="ST_LIR_001" type="Audio">\n'
                    f'          <createdOn>{now}</createdOn>\n'
                    f'          <credential type="token">'
                    f'streamtouch-lir-token</credential>\n'
                    f'          <name></name>\n'
                    f'          <sourceproviderid>11</sourceproviderid>\n'
                    f'          <sourcename>'
                    f'LOCAL_INTERNET_RADIO</sourcename>\n'
                    f'          <sourceSettings/>\n'
                    f'          <updatedOn>{now}</updatedOn>\n'
                    f'          <username></username>\n'
                    f'        </source>\n'
                    f'        <sourceid>ST_LIR_001</sourceid>\n'
                    f'        <updatedOn>{now}</updatedOn>\n'
                    f'      </recent>\n'
                )
            devices_xml += (
                f'    <device deviceid="{device_id}">\n'
                f'      <createdOn>{now}</createdOn>\n'
                f'      <ipaddress>{request.remote_addr}</ipaddress>\n'
                f'      <name></name>\n'
                f'      <updatedOn>{now}</updatedOn>\n'
                f'      <presets/>\n'
                f'      <recents>\n'
                f'{recents_items_xml}'
                f'      </recents>\n'
                f'    </device>\n'
            )
    else:
        devices_xml = (
            f'    <device deviceid="DEFAULT">\n'
            f'      <createdOn>{now}</createdOn>\n'
            f'      <ipaddress>{request.remote_addr}</ipaddress>\n'
            f'      <name></name>\n'
            f'      <updatedOn>{now}</updatedOn>\n'
            f'      <presets/>\n'
            f'      <recents/>\n'
            f'    </device>\n'
        )

    response_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<account id="{account_id}">\n'
        f'  <accountStatus>ACTIVE</accountStatus>\n'
        f'  <mode>global</mode>\n'
        f'  <preferredLanguage>en</preferredLanguage>\n'
        f'  <devices>\n'
        f'{devices_xml}'
        f'  </devices>\n'
        f'  <sources>\n'
        f'    <source id="ST_LIR_001" type="Audio">\n'
        f'      <createdOn>{now}</createdOn>\n'
        f'      <credential type="token">'
        f'streamtouch-lir-token</credential>\n'
        f'      <name>StreamTouch Radio</name>\n'
        f'      <sourceproviderid>11</sourceproviderid>\n'
        f'      <sourcename>LOCAL_INTERNET_RADIO</sourcename>\n'
        f'      <sourceSettings/>\n'
        f'      <updatedOn>{now}</updatedOn>\n'
        f'      <username></username>\n'
        f'    </source>\n'
        f'  </sources>\n'
        f'</account>'
    )
    return Response(
        response_xml,
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml",
        headers={"METHOD_NAME": "getFullAccount"}
    )


@app.route(
    "/streaming/account/<account_id>/device/",
    methods=["GET", "POST"]
)
@app.route(
    "/streaming/account/<account_id>/device/<device_id>",
    methods=["GET", "POST", "PUT"]
)
def streaming_account_device(account_id, device_id=None):
    """
    Device registration. Returns Credentials header so
    speaker stores our token for subsequent calls.
    """
    body = request.data.decode("utf-8", errors="ignore")

    if not device_id:
        match = re.search(r'deviceid="([^"]+)"', body)
        device_id = match.group(1) if match else "UNKNOWN"

    log.info(
        f"Account device: id={account_id} "
        f"device={device_id} method={request.method} "
        f"from {request.remote_addr}"
    )

    now = "2026-01-01T00:00:00.000+00:00"
    shim_base = f"http://{SHIM_HOST}:{SHIM_PORT}"

    response_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<device deviceid="{device_id}">\n'
        f'  <createdOn>{now}</createdOn>\n'
        f'  <ipaddress>{request.remote_addr}</ipaddress>\n'
        f'  <name></name>\n'
        f'  <updatedOn>{now}</updatedOn>\n'
        f'</device>'
    )

    status = 201 if request.method == "POST" else 200

    return Response(
        response_xml,
        status=status,
        mimetype="application/vnd.bose.streaming-v1.2+xml",
        headers={
            "Credentials": f"Bearer {SHIM_TOKEN}",
            "Location": (
                f"{shim_base}/streaming/account"
                f"/{account_id}/device/{device_id}"
            ),
            "METHOD_NAME": "addDevice"
        }
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/recents",
    methods=["GET"]
)
def streaming_account_recents(account_id, device_id):
    """Return stored recents for this device."""
    log.info(
        f"Recents GET: account={account_id} device={device_id} "
        f"from {request.remote_addr}"
    )
    device_recents = recent_store.get(device_id, {})
    now = "2026-01-01T00:00:00.000+00:00"

    items_xml = ""
    for rid, item in list(device_recents.items())[-10:]:
        name = escape_xml(item.get("name", ""))
        location = escape_xml(item.get("location", ""))
        ctype = escape_xml(
            item.get("contentItemType", "stationurl")
        )
        last_played = item.get("lastplayedat", now)
        items_xml += (
            f'  <recent id="{rid}">\n'
            f'    <contentItemType>{ctype}</contentItemType>\n'
            f'    <createdOn>{now}</createdOn>\n'
            f'    <lastplayedat>{last_played}</lastplayedat>\n'
            f'    <location>{location}</location>\n'
            f'    <name>{name}</name>\n'
            f'    <source id="ST_LIR_001" type="Audio">\n'
            f'      <createdOn>{now}</createdOn>\n'
            f'      <credential type="token">'
            f'streamtouch-lir-token</credential>\n'
            f'      <name></name>\n'
            f'      <sourceproviderid>11</sourceproviderid>\n'
            f'      <sourcename>LOCAL_INTERNET_RADIO</sourcename>\n'
            f'      <sourceSettings/>\n'
            f'      <updatedOn>{now}</updatedOn>\n'
            f'      <username></username>\n'
            f'    </source>\n'
            f'    <sourceid>ST_LIR_001</sourceid>\n'
            f'    <updatedOn>{now}</updatedOn>\n'
            f'  </recent>\n'
        )

    response_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<recents>\n{items_xml}</recents>'
    )
    return Response(
        response_xml,
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/recent",
    methods=["POST"]
)
def streaming_account_recent_add(account_id, device_id):
    """
    Speaker POSTs here when something plays.
    Store name and echo back so LED display works correctly.
    The <name> field in our response drives what shows on the display.
    """
    body = request.data.decode("utf-8", errors="ignore")
    log.info(
        f"Recent POST: account={account_id} device={device_id} "
        f"from {request.remote_addr} body={body[:300]}"
    )

    # Extract fields from POST body
    name = ""
    location = ""
    content_item_type = "stationurl"
    last_played_at = "2026-01-01T00:00:00.000+00:00"

    name_match = re.search(r'<name>(.*?)</name>', body)
    if name_match:
        name = name_match.group(1)

    location_match = re.search(
        r'<location>(.*?)</location>', body
    )
    if location_match:
        location = location_match.group(1)

    type_match = re.search(
        r'<contentItemType>(.*?)</contentItemType>', body
    )
    if type_match:
        content_item_type = type_match.group(1)

    played_match = re.search(
        r'<lastplayedat>(.*?)</lastplayedat>', body
    )
    if played_match:
        last_played_at = played_match.group(1)

    log.info(
        f"Recent stored: name='{name}' "
        f"location='{location[:60]}'"
    )

    # Store it — keep last 10 per device
    recent_id = str(int(time.time() * 1000))
    if device_id not in recent_store:
        recent_store[device_id] = {}

    if len(recent_store[device_id]) >= 10:
        oldest = min(recent_store[device_id].keys())
        del recent_store[device_id][oldest]

    item = {
        "name": name,
        "location": location,
        "contentItemType": content_item_type,
        "lastplayedat": last_played_at
    }
    recent_store[device_id][recent_id] = item

    shim_base = f"http://{SHIM_HOST}:{SHIM_PORT}"

    return Response(
        build_recent_xml(recent_id, item),
        status=201,
        mimetype="application/vnd.bose.streaming-v1.2+xml",
        headers={
            "Location": (
                f"{shim_base}/streaming/account/{account_id}"
                f"/device/{device_id}/recent/{recent_id}"
            )
        }
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/recent/<recent_id>",
    methods=["GET"]
)
def streaming_account_recent_get(
    account_id, device_id, recent_id
):
    """Return a specific recent item by ID."""
    log.info(
        f"Recent GET: id={recent_id} device={device_id} "
        f"from {request.remote_addr}"
    )
    device_recents = recent_store.get(device_id, {})
    item = device_recents.get(recent_id)

    if not item:
        return Response(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<status><message>Not found</message>'
            '<status-code>404</status-code></status>',
            status=404,
            mimetype="application/vnd.bose.streaming-v1.2+xml"
        )

    return Response(
        build_recent_xml(recent_id, item),
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/presets",
    methods=["GET"]
)
def streaming_account_presets(account_id, device_id):
    """Device presets — return empty list."""
    log.info(
        f"Presets: account={account_id} device={device_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<presets/>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/preset/<int:button_number>",
    methods=["GET", "PUT", "DELETE"]
)
def streaming_account_preset(
    account_id, device_id, button_number
):
    body = request.data.decode("utf-8", errors="ignore")
    log.info(
        f"Preset {button_number}: account={account_id} "
        f"device={device_id} method={request.method} "
        f"from {request.remote_addr} body={body[:200]}"
    )

    if request.method == "DELETE":
        return Response(
            '',
            status=200,
            mimetype="application/vnd.bose.streaming-v1.2+xml"
        )

    if request.method == "PUT":
        # Wave syncs currently playing station here
        # Must return 200 with preset XML or display breaks
        now = "2026-01-01T00:00:00.000+00:00"
        shim_base = f"http://{SHIM_HOST}:{SHIM_PORT}"

        # Extract fields from PUT body
        name = ""
        location = ""
        content_type = "stationurl"

        name_match = re.search(r'<name>(.*?)</name>', body)
        if name_match:
            name = escape_xml(name_match.group(1))

        location_match = re.search(
            r'<location>(.*?)</location>', body
        )
        if location_match:
            location = escape_xml(location_match.group(1))

        type_match = re.search(
            r'<contentItemType>(.*?)</contentItemType>', body
        )
        if type_match:
            content_type = escape_xml(type_match.group(1))

        log.info(
            f"Preset PUT accepted: slot={button_number} "
            f"name='{name}'"
        )

        response_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<preset buttonNumber="{button_number}">\n'
            f'  <containerArt></containerArt>\n'
            f'  <contentItemType>{content_type}</contentItemType>\n'
            f'  <createdOn>{now}</createdOn>\n'
            f'  <location>{location}</location>\n'
            f'  <name>{name}</name>\n'
            f'  <source id="ST_LIR_001" type="Audio">\n'
            f'    <createdOn>{now}</createdOn>\n'
            f'    <credential type="token">'
            f'streamtouch-lir-token</credential>\n'
            f'    <name></name>\n'
            f'    <sourceproviderid>11</sourceproviderid>\n'
            f'    <sourcename>LOCAL_INTERNET_RADIO</sourcename>\n'
            f'    <sourceSettings/>\n'
            f'    <updatedOn>{now}</updatedOn>\n'
            f'    <username></username>\n'
            f'  </source>\n'
            f'  <updatedOn>{now}</updatedOn>\n'
            f'  <username>{name}</username>\n'
            f'</preset>'
        )

        return Response(
            response_xml,
            status=200,
            mimetype="application/vnd.bose.streaming-v1.2+xml",
            headers={
                "Location": (
                    f"{shim_base}/streaming/account/{account_id}"
                    f"/device/{device_id}/preset/{button_number}"
                ),
                "METHOD_NAME": "updatePreset"
            }
        )

    # GET
    return Response(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<status><message>Not found</message>'
        f'<status-code>404</status-code></status>',
        status=404,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/device/"
    "<device_id>/group/",
    methods=["GET"]
)
def streaming_account_device_group(account_id, device_id):
    """Device group — return empty group."""
    log.info(
        f"Device group: account={account_id} device={device_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<group/>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


@app.route(
    "/streaming/account/<account_id>/provider_settings",
    methods=["GET"]
)
def streaming_provider_settings(account_id):
    """Provider settings — return empty 200."""
    log.info(
        f"Provider settings: account={account_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml",
        headers={"METHOD_NAME": "getProviderSettings"}
    )


@app.route(
    "/streaming/device/<device_id>/streaming_token",
    methods=["GET"]
)
def streaming_device_token(device_id):
    """Returns refreshed token in Authorization header."""
    log.info(
        f"Streaming token: device={device_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '',
        status=200,
        headers={"Authorization": f"Bearer {SHIM_TOKEN}"}
    )


@app.route(
    "/streaming/software/update/account/<account_id>",
    methods=["GET"]
)
def streaming_software_update(account_id):
    """Software update check — no update available."""
    log.info(
        f"Software update: account={account_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<software_update>'
        '<softwareUpdateLocation></softwareUpdateLocation>'
        '</software_update>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )


# Catch-all for any other /streaming/* the speaker calls
@app.route(
    "/streaming/<path:subpath>",
    methods=["GET", "POST", "PUT"]
)
def streaming_catchall(subpath):
    body = request.data.decode("utf-8", errors="ignore")
    log.info(
        f"Streaming catchall /{subpath} from {request.remote_addr} "
        f"method={request.method} body={body[:200]}"
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<status>success</status>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )

# ─── envswitch endpoints ──────────────────────────────────────────────────────

@app.route("/stats", methods=["GET", "POST"])
@app.route("/stats/", methods=["GET", "POST"])
def stats():
    """ST10/Wave posts diagnostic stats here."""
    log.info(
        f"Stats from {request.remote_addr} method={request.method}"
    )
    return Response(
        '{"status":"ok"}',
        status=200,
        mimetype="application/json"
    )


@app.route("/stats/v1/blacklist/<device_id>", methods=["GET"])
def stats_blacklist(device_id):
    """
    Wave checks this on boot and after pairing.
    Returns 404 → Wave disables LED display metadata updates.
    Must return 200 with empty blacklist.
    """
    log.info(
        f"Stats blacklist: device={device_id} "
        f"from {request.remote_addr}"
    )
    return Response(
        '{"blacklisted":false,"features":[]}',
        status=200,
        mimetype="application/json"
    )


@app.route("/stats/v1/<path:subpath>", methods=["GET", "POST"])
def stats_v1_catchall(subpath):
    """Catch-all for any other /stats/v1/* endpoints."""
    body = request.data.decode("utf-8", errors="ignore")
    log.info(
        f"Stats v1 /{subpath} from {request.remote_addr} "
        f"method={request.method} body={body[:100]}"
    )
    return Response(
        '{"status":"ok"}',
        status=200,
        mimetype="application/json"
    )


@app.route("/updates/soundtouch")
@app.route("/updates/soundtouch/<path:subpath>")
def updates(subpath=None):
    log.info(
        f"Updates from {request.remote_addr} subpath={subpath}"
    )
    return jsonify({
        "updates": [],
        "updateAvailable": False,
        "currentVersion": None
    }), 200

# ─── Orion Station API ────────────────────────────────────────────────────────

@app.route("/orion/station")
def orion_station():
    data = request.args.get("data", "")
    log.info(
        f"Orion station from {request.remote_addr} "
        f"data={data[:100]}"
    )
    try:
        padding = 4 - len(data) % 4
        if padding != 4:
            data += "=" * padding
        decoded = base64.b64decode(data).decode("utf-8")
        station = json.loads(decoded)
        name = station.get("name", "Unknown")
        stream_url = station.get("streamUrl", "")
        image_url = station.get("imageUrl", "")
        log.info(f"Orion station: {name} → {stream_url}")
        response_data = {
            "audio": {
                "hasPlaylist": False,
                "isRealtime": True,
                "streamUrl": stream_url
            },
            "imageUrl": image_url,
            "name": name,
            "streamType": "liveRadio"
        }
        return Response(
            json.dumps(response_data),
            status=200,
            mimetype="application/json",
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Access-Control-Allow-Origin": "*"
            }
        )
    except Exception as e:
        log.error(f"Orion station error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/orion/token", methods=["GET", "POST"])
def orion_token():
    log.info("Token requested")
    return jsonify({
        "token": "streamtouch-anonymous-token",
        "expiresIn": 86400
    })


@app.route("/orion/account", methods=["GET", "POST"])
def orion_account():
    log.info("Account requested")
    return jsonify({
        "id": "anonymous",
        "type": "anonymous",
        "token": "streamtouch-anonymous-token"
    })


@app.route("/orion/navigate", methods=["GET", "POST"])
def orion_navigate():
    log.info(f"Navigate from {request.remote_addr}")
    return jsonify({"items": [], "total": 0})


@app.route("/orion/availability", methods=["GET"])
@app.route("/availability", methods=["GET"])
def availability():
    log.info(f"Availability from {request.remote_addr}")
    return Response(
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<status>success</status>',
        status=200,
        mimetype="application/vnd.bose.streaming-v1.2+xml"
    )

# ─── UPnP HTTP Routes ─────────────────────────────────────────────────────────

@app.route("/DeviceDescription.xml")
def device_description():
    log.info("Device description requested")
    return Response(get_device_description(), mimetype="text/xml")


@app.route("/ContentDirectory/<shim_id>/scpd.xml")
def content_directory_scpd(shim_id):
    return Response(CONTENT_DIRECTORY_SCPD, mimetype="text/xml")


@app.route("/ConnectionManager/<shim_id>/scpd.xml")
def connection_manager_scpd(shim_id):
    return Response(CONNECTION_MANAGER_SCPD, mimetype="text/xml")


@app.route(
    "/ConnectionManager/<shim_id>/control.xml",
    methods=["POST"]
)
def connection_manager_control(shim_id):
    return Response(
        """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"
            xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetProtocolInfoResponse
        xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">
      <Source>http-get:*:audio/mpeg:*,http-get:*:audio/mp4:*,\
http-get:*:audio/aac:*,http-get:*:audio/ogg:*,\
http-get:*:audio/flac:*,http-get:*:audio/wav:*</Source>
      <Sink></Sink>
    </u:GetProtocolInfoResponse>
  </s:Body>
</s:Envelope>""",
        mimetype="text/xml"
    )


@app.route(
    "/ContentDirectory/<shim_id>/control.xml",
    methods=["POST"]
)
def content_directory_control(shim_id):
    body = request.data.decode("utf-8")
    log.info(f"ContentDirectory request: {body[:300]}")
    presets = load_presets()
    object_id_match = re.search(
        r'<ObjectID[^>]*>(.*?)</ObjectID>', body
    )
    browse_flag_match = re.search(
        r'<BrowseFlag[^>]*>(.*?)</BrowseFlag>', body
    )
    object_id = (
        object_id_match.group(1).strip()
        if object_id_match else "0"
    )
    browse_flag = (
        browse_flag_match.group(1).strip()
        if browse_flag_match else "BrowseDirectChildren"
    )
    log.info(f"ObjectID={object_id} BrowseFlag={browse_flag}")
    if object_id == "0":
        content = build_didl_container(
            "st_music", "0",
            "StreamTouch Music Assistant",
            len(presets)
        )
        return Response(
            build_soap_browse_response(content, 1, 1),
            mimetype="text/xml"
        )
    elif object_id == "st_music":
        items = []
        for preset_id, preset in presets.items():
            stream_url = (
                f"http://{SHIM_HOST}:{SHIM_PORT}/stream/{preset_id}"
            )
            items.append(build_didl_item(
                preset_id, "st_music",
                preset.get("name", "Unknown"),
                stream_url,
                preset.get("artwork")
            ))
        content = "".join(items)
        return Response(
            build_soap_browse_response(
                content, len(items), len(items)
            ),
            mimetype="text/xml"
        )
    elif object_id in presets:
        preset = presets[object_id]
        stream_url = (
            f"http://{SHIM_HOST}:{SHIM_PORT}/stream/{object_id}"
        )
        content = build_didl_item(
            object_id, "st_music",
            preset.get("name", "Unknown"),
            stream_url,
            preset.get("artwork")
        )
        return Response(
            build_soap_browse_response(content, 1, 1),
            mimetype="text/xml"
        )
    else:
        log.warning(f"Unknown ObjectID: {object_id}")
        return Response(
            build_soap_browse_response("", 0, 0),
            mimetype="text/xml"
        )


@app.route("/stream/<preset_id>")
def stream_preset(preset_id):
    presets = load_presets()
    preset = presets.get(preset_id)
    if not preset:
        log.warning(f"Preset not found: {preset_id}")
        return Response("Not found", status=404)
    ma_uri = preset.get("ma_uri")
    log.info(f"Stream request: {preset_id} → {ma_uri}")
    stream_url = get_ma_stream_url(ma_uri)
    if stream_url:
        log.info(f"Redirecting to: {stream_url}")
        return Response(
            status=302,
            headers={"Location": stream_url}
        )
    return Response("Stream unavailable", status=503)

# ─── StreamTouch REST API ─────────────────────────────────────────────────────

@app.route("/api/preset", methods=["POST"])
def register_preset():
    data = request.json
    name = data.get("name", "Unknown")
    ma_uri = data.get("ma_uri", "")
    artwork = data.get("artwork")
    if not ma_uri:
        return jsonify({"error": "ma_uri required"}), 400
    preset_id = "st_" + uuid.uuid5(
        uuid.NAMESPACE_URL, ma_uri
    ).hex[:16]
    presets = load_presets()
    presets[preset_id] = {
        "name": name,
        "ma_uri": ma_uri,
        "artwork": artwork
    }
    save_presets(presets)
    log.info(f"Preset registered: {name} ({preset_id})")
    station_data = base64.b64encode(json.dumps({
        "name": name,
        "imageUrl": artwork or "",
        "streamUrl": ma_uri
    }).encode()).decode()
    orion_url = (
        f"http://{SHIM_HOST}:{SHIM_PORT}"
        f"/orion/station?data={station_data}"
    )
    return jsonify({
        "object_id": preset_id,
        "source_account": f"{SHIM_UUID}/0",
        "shim_host": SHIM_HOST,
        "shim_port": SHIM_PORT,
        "orion_url": orion_url,
        "upnp_location": (
            f"http://{SHIM_HOST}:{SHIM_PORT}/stream/{preset_id}"
        )
    })


@app.route("/api/preset", methods=["GET"])
def list_presets():
    return jsonify(load_presets())


@app.route("/api/preset/<preset_id>", methods=["DELETE"])
def delete_preset(preset_id):
    presets = load_presets()
    if preset_id in presets:
        del presets[preset_id]
        save_presets(presets)
        return jsonify({"status": "deleted"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "shim_uuid": SHIM_UUID,
        "shim_token": SHIM_TOKEN,
        "ma_host": MA_HOST,
        "ma_port": MA_PORT,
        "preset_count": len(load_presets()),
        "recent_devices": list(recent_store.keys()),
        "registry_url_v1": (
            f"http://{SHIM_HOST}:{SHIM_PORT}"
            f"/bmx/registry/v1/services"
        ),
        "orion_base_url": (
            f"http://{SHIM_HOST}:{SHIM_PORT}/orion"
        ),
        # Both variants use identical 5-command sequence
        # Confirmed from Wireshark captures of Wave/lisa and ST10/rhino
        "telnet_commands": [
            f"sys configuration bmxRegistryUrl "
            f"http://{SHIM_HOST}:{SHIM_PORT}"
            f"/bmx/registry/v1/services",
            f"sys configuration statsServerUrl "
            f"http://{SHIM_HOST}:{SHIM_PORT}/stats",
            "envswitch AccountId set 6426718741759999998",
            f"envswitch boseurls set "
            f"http://{SHIM_HOST}:{SHIM_PORT} "
            f"http://{SHIM_HOST}:{SHIM_PORT}/updates/soundtouch",
            "sys reboot"
        ]
    })

# ─── mDNS advertisement ───────────────────────────────────────────────────────

def advertise_mdns():
    try:
        zc = Zeroconf()
        local_ip = socket.inet_aton(SHIM_HOST)
        info = ServiceInfo(
            "_upnp._tcp.local.",
            "StreamTouch Music Assistant._upnp._tcp.local.",
            addresses=[local_ip],
            port=SHIM_PORT,
            properties={
                b"deviceType": (
                    b"urn:schemas-upnp-org:device:MediaServer:1"
                ),
                b"friendlyName": b"StreamTouch Music Assistant",
                b"uuid": SHIM_UUID.encode(),
                b"location": (
                    f"http://{SHIM_HOST}:{SHIM_PORT}"
                    f"/DeviceDescription.xml"
                ).encode()
            }
        )
        zc.register_service(info)
        log.info(
            "mDNS service registered: StreamTouch Music Assistant"
        )
        while True:
            time.sleep(60)
    except Exception as e:
        log.error(f"mDNS error: {e}")

# ─── SSDP advertisement ───────────────────────────────────────────────────────

def advertise_ssdp():
    SSDP_ADDR = "239.255.255.250"
    SSDP_PORT = 1900
    alive_msg = (
        f"NOTIFY * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        f"CACHE-CONTROL: max-age=1800\r\n"
        f"LOCATION: http://{SHIM_HOST}:{SHIM_PORT}"
        f"/DeviceDescription.xml\r\n"
        f"NT: urn:schemas-upnp-org:device:MediaServer:1\r\n"
        f"NTS: ssdp:alive\r\n"
        f"SERVER: Linux/1.0 UPnP/1.1 StreamTouch/1.0\r\n"
        f"USN: uuid:{SHIM_UUID}::"
        f"urn:schemas-upnp-org:device:MediaServer:1\r\n"
        f"\r\n"
    ).encode()
    sock = socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    while True:
        try:
            sock.sendto(alive_msg, (SSDP_ADDR, SSDP_PORT))
            log.info("SSDP:alive sent")
        except Exception as e:
            log.error(f"SSDP error: {e}")
        time.sleep(30)

# ─── SSDP M-SEARCH handler ────────────────────────────────────────────────────

def handle_ssdp_msearch():
    SSDP_ADDR = "239.255.255.250"
    SSDP_PORT = 1900
    try:
        sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", SSDP_PORT))
        mreq = (
            socket.inet_aton(SSDP_ADDR)
            + socket.inet_aton(SHIM_HOST)
        )
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq
        )
        log.info("SSDP M-SEARCH listener started on port 1900")
        while True:
            try:
                data, addr = sock.recvfrom(2048)
                msg = data.decode("utf-8", errors="ignore")
                if "M-SEARCH" in msg:
                    if any(x in msg for x in [
                        "ssdp:all", "MediaServer",
                        "upnp:rootdevice", "device:MediaServer"
                    ]):
                        response = (
                            f"HTTP/1.1 200 OK\r\n"
                            f"CACHE-CONTROL: max-age=1800\r\n"
                            f"DATE: "
                            f"{time.strftime('%a, %d %b %Y %H:%M:%S GMT')}"
                            f"\r\n"
                            f"EXT:\r\n"
                            f"LOCATION: http://{SHIM_HOST}:{SHIM_PORT}"
                            f"/DeviceDescription.xml\r\n"
                            f"SERVER: Linux/1.0 UPnP/1.1 StreamTouch/1.0\r\n"
                            f"ST: urn:schemas-upnp-org"
                            f":device:MediaServer:1\r\n"
                            f"USN: uuid:{SHIM_UUID}::"
                            f"urn:schemas-upnp-org"
                            f":device:MediaServer:1\r\n"
                            f"\r\n"
                        ).encode()
                        resp_sock = socket.socket(
                            socket.AF_INET, socket.SOCK_DGRAM
                        )
                        resp_sock.sendto(response, addr)
                        resp_sock.close()
                        log.info(
                            f"M-SEARCH response sent to {addr[0]}"
                        )
            except Exception as e:
                log.error(f"M-SEARCH receive error: {e}")
    except Exception as e:
        log.error(f"M-SEARCH listener failed to start: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("StreamTouch UPnP Shim starting...")
    log.info(f"UUID:               {SHIM_UUID}")
    log.info(f"Token:              {SHIM_TOKEN}")
    log.info(f"Listening:          http://{SHIM_HOST}:{SHIM_PORT}")
    log.info(f"MA:                 http://{MA_HOST}:{MA_PORT}")
    log.info(
        f"Registry (ST10/Wave): "
        f"http://{SHIM_HOST}:{SHIM_PORT}/bmx/registry/v1/services"
    )
    log.info(
        f"Source providers:   "
        f"http://{SHIM_HOST}:{SHIM_PORT}/streaming/sourceproviders"
    )
    log.info(
        f"Account full:       "
        f"http://{SHIM_HOST}:{SHIM_PORT}"
        f"/streaming/account/{{id}}/full"
    )
    log.info(
        f"Recents:            "
        f"http://{SHIM_HOST}:{SHIM_PORT}"
        f"/streaming/account/{{id}}/device/{{did}}/recent"
    )
    log.info(
        f"Orion API:          "
        f"http://{SHIM_HOST}:{SHIM_PORT}/orion"
    )

    threading.Thread(target=advertise_mdns, daemon=True).start()
    threading.Thread(target=advertise_ssdp, daemon=True).start()
    threading.Thread(target=handle_ssdp_msearch, daemon=True).start()

    app.run(host="0.0.0.0", port=SHIM_PORT, debug=False)
