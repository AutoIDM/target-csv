#!/usr/bin/env python3

import argparse
import io
import os
import sys
import json
import csv
import threading
import http.client
import urllib
from datetime import datetime
import collections
import pkg_resources
import paramiko
import base64
from jsonschema.validators import Draft4Validator
import singer

logger = singer.get_logger()


def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug('Emitting state {}'.format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


def flatten(d, parent_key='', sep='__'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, str(v) if type(v) is list else v))
    return dict(items)


def persist_messages(delimiter, quotechar, messages, destination_path, 
                    fixed_headers, sftp_host, sftp_username, sftp_password, 
                    sftp_port, sftp_public_key, sftp_public_key_format):

    state = None
    schemas = {}
    stream_2_filenames = {}
    key_properties = {}
    headers = {}
    validators = {}

    now = datetime.now().strftime('%Y%m%dT%H%M%S')

    for message in messages:
        try:
            o = singer.parse_message(message).asdict()
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(message))
            raise
        message_type = o['type']
        if message_type == 'RECORD':
            if o['stream'] not in schemas:
                raise Exception("A record for stream {}"
                                "was encountered before a corresponding schema".format(o['stream']))

            validators[o['stream']].validate(o['record'])

            filename = o['stream'] + '-' + now + '.csv'
            stream_2_filenames[o['stream']] = filename
            filename = os.path.expanduser(os.path.join(destination_path, filename))
            file_is_empty = (not os.path.isfile(filename)) or os.stat(filename).st_size == 0

            # flattened_record = flatten(o['record'])
            flattened_record = o['record']

            if fixed_headers is not None and o['stream'] in fixed_headers:
                if o['stream'] not in headers:
                    headers[o['stream']] = fixed_headers[o['stream']]
            else:
                if o['stream'] not in headers and not file_is_empty:
                    with open(filename, 'r') as csvfile:
                        reader = csv.reader(csvfile,
                                            delimiter=delimiter,
                                            quotechar=quotechar)
                        first_line = next(reader)
                        headers[o['stream']] = first_line if first_line else flattened_record.keys()
                else:
                    headers[o['stream']] = flattened_record.keys()

            with open(filename, 'a') as csvfile:
                writer = csv.DictWriter(csvfile,
                                        headers[o['stream']],
                                        extrasaction='ignore',
                                        delimiter=delimiter,
                                        quotechar=quotechar)
                if file_is_empty:
                    writer.writeheader()

                writer.writerow(flattened_record)

            state = None
        elif message_type == 'STATE':
            logger.debug('Setting state to {}'.format(o['value']))
            state = o['value']
        elif message_type == 'SCHEMA':
            stream = o['stream']
            schemas[stream] = o['schema']
            validators[stream] = Draft4Validator(o['schema'])
            key_properties[stream] = o['key_properties']
        else:
            logger.warning("Unknown message type {} in message {}"
                            .format(o['type'], o))
    if(sftp_host and sftp_password and sftp_username and sftp_public_key and sftp_public_key_format):
        sftp = None
        client = None
        try: 
            #key = paramiko.RSAKey(data=base64.b64decode(sftp_public_key))
            client = paramiko.SSHClient()
            #client.get_host_keys().add(sftp_host, sftp_public_key_format, key)
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) #NOT SECURE!
            client.connect(hostname=sftp_host,username=sftp_username,password=sftp_password)
            sftp = client.open_sftp()
            for filename in stream_2_filenames.values():
                sftp.put(filename,filename)
                logger.info(f"File Name: {filename}. pushed to SFTP Site")
        except Exception as e:
            if (sftp != None): sftp.close()
            if (client != None): client.close()
            raise e
        else:
            sftp.close()
            client.close()
    return state


def send_usage_stats():
    try:
        version = pkg_resources.get_distribution('target-csv').version
        conn = http.client.HTTPConnection('collector.singer.io', timeout=10)
        conn.connect()
        params = {
            'e': 'se',
            'aid': 'singer',
            'se_ca': 'target-csv',
            'se_ac': 'open',
            'se_la': version,
        }
        conn.request('GET', '/i?' + urllib.parse.urlencode(params))
        response = conn.getresponse()
        conn.close()
    except:
        logger.debug('Collection request failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', help='Config file')
    args = parser.parse_args()

    if args.config:
        with open(args.config) as input_json:
            config = json.load(input_json)
    else:
        config = {}

    if not config.get('disable_collection', False):
        logger.info('Sending version information to singer.io. ' +
                    'To disable sending anonymous usage data, set ' +
                    'the config parameter "disable_collection" to true')
        threading.Thread(target=send_usage_stats).start()

    input_messages = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = persist_messages(config.get('delimiter', ','),
                             config.get('quotechar', '"'),
                             input_messages,
                             config.get('destination_path', ''),
                             config.get('fixed_headers'),
                             config.get('sftp_host'),
                             config.get('sftp_username'),
                             config.get('sftp_password'),
                             config.get('sftp_port'),
                             config.get('sftp_public_key'),
                             config.get('sftp_public_key_format'),
                             )

    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == '__main__':
    main()
