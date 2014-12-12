import collections

import json
import os
import requests

DEFAULT_PORT=7934

class ApiException(Exception):
    def __init__(self, response):
        self.response = response
    def __str__(self):
        return "API Exception: %s, %s" % (self.response.status_code, self.response.content)

def convert(data):
    if isinstance(data, basestring):
        return data.encode('utf-8')
    elif isinstance(data, collections.Mapping):
        return dict(map(convert, data.iteritems()))
    elif isinstance(data, collections.Iterable):
        return type(data)(map(convert, data))
    else:
        return data

class CtrlKApi(object):
    def __init__(self, host='127.0.0.1', port=DEFAULT_PORT):
        self.host=host
        self.port=port
        self.project_root = None

    @property
    def base_url(self):
        return 'http://' + self.host + ':' + str(self.port)

    def get_url(self, path):
        return os.path.join(self.base_url, path)

    def safe_get(self, path, *args, **kwargs):
        if self.project_root is not None:
            if 'params' not in kwargs:
                kwargs['params'] = {}
            kwargs['params']['project_root'] = self.project_root
        r = requests.get(self.get_url(path), *args, **kwargs)
        if not r.ok:
            assert r.status_code != 200
            raise ApiException(r)
        return r

    def safe_post(self, path, *args, **kwargs):
        if self.project_root is not None:
            if 'data' not in kwargs:
                kwargs['data'] = {}
            kwargs['data']['project_root'] = self.project_root
        r = requests.post(self.get_url(path), *args, **kwargs)
        if not r.ok:
            assert r.status_code != 200
            raise ApiException(r)
        return r

    def register(self, library_path, project_root):
        self.safe_get('register', params={'project_root' : project_root, 'library_path' : library_path})
        self.project_root = project_root
        return None

    def parse(self, file_name=None):
        payload = {}
        if file_name:
            payload['file_name'] = file_name
        self.safe_get('parse', params=payload)

    def get_queue_size(self):
        return convert(self.safe_get('queue_size').json())

    def leveldb_search(self, starts_with):
        return convert(self.safe_get('leveldb_search', params={'starts_with' : starts_with}).json())

    def get_items_matching_pattern(self, prefix, limit):
        return convert(self.safe_get('match', params={'prefix' : prefix, 'limit' : limit}).json())

    def get_builtin_header_path(self):
        return convert(self.safe_get('builtin_header_path').json())

    def get_file_args(self, file_name):
        return convert(self.safe_get('file_args', params={'file_name' : file_name}).json())

    def parse_current_file(self, command, file_name, content):
        payload = {'command': json.dumps(command), 'file_name': file_name, 'content': content}
        self.safe_post('parse_current_file', data=payload)

    def unload_current_file(self, file_name):
        payload = {'file_name': file_name}
        self.safe_get('unload_current_file', params=payload)

    def get_usr_under_cursor(self, file_name, row, col):
        payload = {'file_name': file_name, 'row': row, 'col': col}
        return convert(self.safe_get('get_usr_under_cursor', params=payload).json())

    def get_current_scope_str(self, file_name, row):
        payload = {'file_name': file_name, 'row': row}
        return convert(self.safe_get('get_current_scope_str', params=payload).json())
